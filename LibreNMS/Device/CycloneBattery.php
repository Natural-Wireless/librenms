<?php

/**
 * CycloneBattery.php
 *
 * Cyclone CYG-B48V-100A LiFePO4 batteries (PACE BMS) wired to a Moxa NPort serial server,
 * shown as native sensors on the NPort's device.
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>.
 *
 * @link       https://www.librenms.org
 */

namespace LibreNMS\Device;

use App\Facades\DeviceCache;
use App\Facades\LibrenmsConfig;
use App\Models\Device;
use App\Models\Sensor;
use App\Models\StateIndex;
use App\Models\StateTranslation;
use Illuminate\Support\Facades\Log;
use LibreNMS\Util\Url;
use Symfony\Component\Process\Process;

/**
 * The battery protocol lives in scripts/cyclone_bms.py, one decoder tested against real PACE
 * captures. Its --librenms mode prints every sensor (class, index, descr, limits, value) and
 * the state tables; this class turns that into sensors at discovery and values when polling.
 *
 * Every NPort is auto-detected: discovery asks it for a bank reading (-a 1 --bank) and keeps
 * the battery sensors only if a battery answers. Once found, the packs it has seen stay
 * (--min-packs), so a pack that stops answering alarms instead of disappearing.
 * Turn detection off with: lnms config:set --ignore-checks cyclone_bms_detect false
 *
 * Per-NPort overrides, which win over detection:
 *   lnms config:set --ignore-checks cyclone_bms.+ '{"device":"<nport-host>","options":"-a 1 --bank --packs 3"}'
 *   lnms config:set --ignore-checks cyclone_bms.+ '{"device":"<other-nport>","options":"off"}'   (never contact it)
 * "device" matches the device's hostname, overwrite IP, sysName or device_id; "options" are
 * cyclone_bms.py options (string or list).
 */
class CycloneBattery
{
    public const TYPE_PREFIX = 'cyclone-bms';

    /**
     * The sensor poller is SNMP-only, so every battery sensor points at sysUpTime.0 and the
     * moxa-nport poller include swaps in the battery value.
     */
    public const OID = '.1.3.6.1.2.1.1.3.0';

    /** html/images/os/cyclone.png (list icon) and html/images/logos/cyclone.png (device page logo) */
    public const ICON = 'cyclone.png';

    /** Device header graphs for an NPort with batteries, drawn by includes/html/graphs/device/cyclone_*.inc.php */
    public const OVERVIEW_GRAPHS = [
        ['graph' => 'device_cyclone_soc', 'text' => 'State of Charge'],
        ['graph' => 'device_cyclone_voltage', 'text' => 'Battery Voltage'],
        ['graph' => 'device_cyclone_current', 'text' => 'Battery Current'],
    ];

    private const STATUS_TYPE = 'cyclone-bms-status';
    private const STATUS_NOT_RESPONDING = 3;
    private const CACHE_SECONDS = 120;
    private const DETECT_OPTIONS = ['-a', '1', '--bank'];

    /** @var array<int, array{time: int, data: ?array}> */
    private static array $cache = [];

    /** @var array<int, array{time: int, packs: int}> */
    private static array $known = [];

    /**
     * cyclone_bms.py options for this device, or null if it must not be contacted.
     *
     * @return string[]|null
     */
    public static function options(array $device): ?array
    {
        $entry = self::entry($device);
        if ($entry !== null) {
            $options = $entry['options'] ?? [];
            if ($options === 'off' || $options === false) {
                return null;
            }

            return is_array($options)
                ? array_values(array_map('strval', $options))
                : preg_split('/\s+/', trim((string) $options), -1, PREG_SPLIT_NO_EMPTY);
        }

        if (! LibrenmsConfig::get('cyclone_bms_detect', true)) {
            return null;
        }

        $packs = self::knownPacks($device);

        // an NPort with nothing found yet gets one connection attempt, not three
        return $packs > 0
            ? array_merge(self::DETECT_OPTIONS, ['--min-packs', (string) $packs])
            : array_merge(self::DETECT_OPTIONS, ['--connect-attempts', '1']);
    }

    /**
     * This device's cyclone_bms config entry, if it has one.
     */
    private static function entry(array $device): ?array
    {
        $names = array_filter([
            (string) ($device['device_id'] ?? ''),
            (string) ($device['hostname'] ?? ''),
            (string) ($device['overwrite_ip'] ?? ''),
            (string) ($device['sysName'] ?? ''),
        ], fn ($name) => $name !== '');

        foreach ((array) LibrenmsConfig::get('cyclone_bms', []) as $entry) {
            if (in_array((string) ($entry['device'] ?? ''), $names, true)) {
                return (array) $entry;
            }
        }

        return null;
    }

    /**
     * How many packs this device already has sensors for (0 = no battery found yet).
     */
    private static function knownPacks(array $device): int
    {
        $id = (int) $device['device_id'];
        $known = self::$known[$id] ?? null;
        if ($known === null || $known['time'] <= time() - self::CACHE_SECONDS) {
            $packs = Sensor::where('device_id', $id)
                ->where('sensor_type', 'like', self::TYPE_PREFIX . '%')
                ->pluck('sensor_index')
                ->map(fn ($index) => preg_match('/^cyclone\.(\d+)\./', (string) $index, $m) ? (int) $m[1] : 0)
                ->max();
            $known = self::$known[$id] = ['time' => time(), 'packs' => (int) $packs];
        }

        return $known['packs'];
    }

    /**
     * Read every battery on the device, once per poll: each sensor class asks in turn.
     */
    public static function read(array $device): ?array
    {
        $options = self::options($device);
        if ($options === null) {
            return null;
        }

        $cached = self::$cache[$device['device_id']] ?? null;
        if ($cached !== null && $cached['time'] > time() - self::CACHE_SECONDS) {
            return $cached['data'];
        }

        $host = ($device['overwrite_ip'] ?? null) ?: $device['hostname'];
        $process = new Process(array_merge([base_path('scripts/cyclone_bms.py'), '-H', $host, '--librenms'], $options));
        $process->setTimeout(120);
        $process->run();

        $data = json_decode($process->getOutput(), true);
        if (! is_array($data) || ! isset($data['sensors'], $data['states'])) {
            Log::error("Cyclone battery read failed for $host: " . trim($process->getErrorOutput() ?: $process->getOutput()));
            $data = null;
        } elseif (! empty($data['error']) && (self::entry($device) !== null || self::knownPacks($device) > 0)) {
            Log::warning("Cyclone battery on $host: {$data['error']}");  // an NPort without a battery is expected to fail
        }

        self::$cache[$device['device_id']] = ['time' => time(), 'data' => $data];

        return $data;
    }

    /**
     * Discover this device's battery sensors of one class (called once per sensor class).
     */
    public static function discover(array $device, string $class): void
    {
        $options = self::options($device);
        $data = $options === null ? null : self::read($device);

        // configured by hand, found before, or answering now; otherwise the NPort has no battery
        $battery = $options !== null
            && (self::entry($device) !== null || self::knownPacks($device) > 0 || ($data['packs_answered'] ?? 0) > 0);

        if ($class === 'state') {
            self::setIcon($device, $battery);
        }

        if (! $battery) {
            return;
        }

        $existing = static::existingSensors($device, $class);

        if ($data === null) {
            // The script itself failed: re-assert what exists so sync doesn't delete it.
            self::rediscover($device, $class, $existing);

            return;
        }

        if ($class === 'state') {
            foreach ($data['states'] as $stateName => $states) {
                create_state_index($stateName, $states);
            }
        }

        foreach ($data['sensors'] as $sensor) {
            if ($sensor['class'] !== $class) {
                continue;
            }

            // discover_sensor() would store a missing value as 0 (a false SOC 0% alarm), so keep
            // the last reading, and leave a sensor that has never had one for a later discovery.
            $value = $sensor['value'] ?? $existing[$sensor['index']]['sensor_current'] ?? null;
            if ($value === null) {
                continue;
            }

            discover_sensor(null, $class, $device, self::OID, $sensor['index'], $sensor['type'], $sensor['descr'],
                1, 1, $sensor['low'], $sensor['low_warn'], $sensor['warn'], $sensor['high'], $value);
        }
    }

    /**
     * The value for one battery sensor while polling; any other sensor keeps its SNMP value.
     */
    public static function pollValue(array $device, array $sensor, mixed $snmpValue): mixed
    {
        if (! str_starts_with((string) $sensor['sensor_type'], self::TYPE_PREFIX)) {
            return $snmpValue;
        }

        if (self::options($device) === null) {
            return $sensor['sensor_current']; // no longer configured: the next discovery removes it
        }

        $data = self::read($device);
        if ($data === null) {
            return $sensor['sensor_type'] === self::STATUS_TYPE ? self::STATUS_NOT_RESPONDING : $sensor['sensor_current'];
        }

        foreach ($data['sensors'] as $candidate) {
            if ($candidate['index'] === $sensor['sensor_index']) {
                // a pack that didn't answer has no reading: hold the last one, its status sensor alarms
                return $candidate['value'] ?? $sensor['sensor_current'];
            }
        }

        return $sensor['sensor_current'];
    }

    /**
     * Header graphs for an NPort that batteries were found on, or null for any other device.
     * Keyed off the icon setIcon() gives it: the icon is loaded with every device, so pages that
     * list many devices need no extra query.
     */
    public static function overviewGraphs(Device $device): ?array
    {
        return ($device->getAttributes()['icon'] ?? null) === self::ICON ? self::OVERVIEW_GRAPHS : null;
    }

    /**
     * Show the Cyclone logo on an NPort with a battery behind it. The OS module applies the
     * override_icon attribute on every poll; it is set here so the logo appears right away.
     */
    private static function setIcon(array $device, bool $battery): void
    {
        $model = DeviceCache::get((int) $device['device_id']);
        $current = $model->getAttrib('override_icon');

        if ($battery && $current !== self::ICON) {
            $model->setAttrib('override_icon', self::ICON);
        } elseif (! $battery && $current === self::ICON) {
            $model->forgetAttrib('override_icon');
        } else {
            return;
        }

        $model->icon = basename(Url::findOsImage($model->os, $model->features, $model->getAttrib('override_icon')));
        $model->save();
    }

    private static function rediscover(array $device, string $class, array $existing): void
    {
        if ($class === 'state') {
            foreach (array_unique(array_column($existing, 'sensor_type')) as $stateName) {
                create_state_index($stateName, static::existingStates($stateName));
            }
        }

        foreach ($existing as $index => $sensor) {
            discover_sensor(null, $class, $device, $sensor['sensor_oid'], $index, $sensor['sensor_type'], $sensor['sensor_descr'],
                1, 1, $sensor['sensor_limit_low'], $sensor['sensor_limit_low_warn'], $sensor['sensor_limit_warn'],
                $sensor['sensor_limit'], $sensor['sensor_current']);
        }
    }

    /**
     * @return array<string, array<string, mixed>> raw sensor rows keyed by sensor_index
     */
    protected static function existingSensors(array $device, string $class): array
    {
        return Sensor::where('device_id', $device['device_id'])
            ->where('sensor_class', $class)
            ->where('sensor_type', 'like', self::TYPE_PREFIX . '%')
            ->get()
            ->mapWithKeys(fn (Sensor $sensor) => [$sensor->sensor_index => $sensor->getAttributes()])
            ->all();
    }

    /**
     * @return array<int, array{value: int, descr: string, generic: int}>
     */
    protected static function existingStates(string $stateName): array
    {
        $index = StateIndex::where('state_name', $stateName)->with('translations')->first();

        return $index?->translations->map(fn (StateTranslation $translation) => [
            'value' => $translation->state_value,
            'descr' => $translation->state_descr,
            'generic' => $translation->state_generic_value,
        ])->all() ?? [];
    }
}
