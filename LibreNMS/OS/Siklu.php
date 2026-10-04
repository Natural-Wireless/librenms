<?php

/**
 * Siklu.php
 *
 * Siklu Communication
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
 *
 * @copyright  2017 Tony Murray
 * @author     Tony Murray <murraytony@gmail.com>
 */

namespace LibreNMS\OS;

use App\Models\Device;
use LibreNMS\Device\WirelessSensor;
use LibreNMS\Enum\WirelessSensorType;
use LibreNMS\Interfaces\Discovery\Sensors\WirelessFrequencyDiscovery;
use LibreNMS\Interfaces\Discovery\Sensors\WirelessPowerDiscovery;
use LibreNMS\Interfaces\Discovery\Sensors\WirelessRateDiscovery;
use LibreNMS\Interfaces\Discovery\Sensors\WirelessRssiDiscovery;
use LibreNMS\Interfaces\Discovery\Sensors\WirelessSnrDiscovery;
use LibreNMS\Interfaces\Polling\Sensors\WirelessRatePolling;
use LibreNMS\OS;
use SnmpQuery;

class Siklu extends OS implements
    WirelessFrequencyDiscovery,
    WirelessPowerDiscovery,
    WirelessRateDiscovery,
    WirelessRatePolling,
    WirelessRssiDiscovery,
    WirelessSnrDiscovery
{
    private const AIR_CAPACITY_OID = '.1.3.6.1.4.1.31926.2.1.1.50.1'; // RADIO-BRIDGE-MIB::rfAirCapacity.1

    // RADIO-BRIDGE-MIB::rfChannelWidth.1, rfModulationType.1, rfNumOfSubchannels.1, rfNumOfRepetitions.1, rfFecRate.1
    private const MODE_OIDS = [
        '.1.3.6.1.4.1.31926.2.1.1.3.1',
        '.1.3.6.1.4.1.31926.2.1.1.7.1',
        '.1.3.6.1.4.1.31926.2.1.1.8.1',
        '.1.3.6.1.4.1.31926.2.1.1.9.1',
        '.1.3.6.1.4.1.31926.2.1.1.10.1',
    ];

    // Air capacity in Mbps from the Siklu product documentation.
    // EH-1200 keys: channel width:modulation:subchannels:repetitions (FEC 0.5 only)
    // EH-2200/2500 keys: channel width:modulation:FEC
    // channel width: 1=250MHz 2=500MHz; modulation: 1=QPSK 2=QAM16 3=QAM64 4=QAM32 5=BPSK; FEC: 1=0.5 2=0.67
    private const EH1200_FDD_CAPACITY = [
        '2:3:4:1' => 1000, '2:2:4:1' => 700, '2:1:4:1' => 350, '2:1:2:2' => 85, '2:1:1:4' => 20,
        '1:3:4:1' => 500, '1:2:4:1' => 350, '1:1:4:1' => 175, '1:1:2:2' => 42, '1:1:1:4' => 10,
    ];

    private const EH1200_TDD_CAPACITY = [
        '2:3:4:1' => 970, '2:2:4:1' => 660, '2:1:4:1' => 340, '2:1:2:2' => 78, '2:1:1:4' => 20,
        '1:3:4:1' => 485, '1:2:4:1' => 330, '1:1:4:1' => 170, '1:1:2:2' => 39, '1:1:1:4' => 10,
    ];

    private const EH2200_CAPACITY = [
        '2:4' => 2000, '2:2' => 1500, '2:1' => 800, '2:5:2' => 200, '2:5:1' => 80,
        '1:4' => 1000, '1:2' => 750, '1:1' => 400, '1:5:2' => 100, '1:5:1' => 40,
    ];

    public function discoverOS(Device $device): void
    {
        $data = snmp_get_multi_oid($this->getDeviceArray(), [
            'rbSwBank1Running.0',
            'rbSwBank1Version.0',
            'rbSwBank2Version.0',
            'entPhysicalSerialNum.1',
        ], '-OQUs', 'RADIO-BRIDGE-MIB:ENTITY-MIB');

        $device->version = $data['rbSwBank1Running.0'] == 'running' ? $data['rbSwBank1Version.0'] : $data['rbSwBank2Version.0'];
        $device->hardware = $device->sysDescr;
        $device->serial = $data['entPhysicalSerialNum.1'];
    }

    /**
     * Discover wireless frequency.  This is in GHz. Type is frequency.
     * Returns an array of LibreNMS\Device\Sensor objects that have been discovered
     *
     * @return array Sensors
     */
    public function discoverWirelessFrequency()
    {
        $oid = '.1.3.6.1.4.1.31926.2.1.1.4.1'; // RADIO-BRIDGE-MIB::rfOperationalFrequency.1

        return [
            new WirelessSensor(WirelessSensorType::Frequency, $this->getDeviceId(), $oid, 'siklu', 1, 'Frequency', null, 1, 1000),
        ];
    }

    /**
     * Discover wireless tx or rx power. This is in dBm. Type is power.
     * Returns an array of LibreNMS\Device\Sensor objects that have been discovered
     *
     * @return array
     */
    public function discoverWirelessPower()
    {
        $oid = '.1.3.6.1.4.1.31926.2.1.1.42.1'; // RADIO-BRIDGE-MIB::rfTxPower.1

        return [
            new WirelessSensor(WirelessSensorType::Power, $this->getDeviceId(), $oid, 'siklu', 1, 'Tx Power'),
        ];
    }

    /**
     * Discover wireless RSSI (Received Signal Strength Indicator). This is in dBm. Type is rssi.
     * Returns an array of LibreNMS\Device\Sensor objects that have been discovered
     *
     * @return array
     */
    public function discoverWirelessRssi()
    {
        $oid = '.1.3.6.1.4.1.31926.2.1.1.19.1'; // RADIO-BRIDGE-MIB::rfAverageRssi.1

        return [
            new WirelessSensor(WirelessSensorType::Rssi, $this->getDeviceId(), $oid, 'siklu', 1, 'RSSI', low_limit: -60, low_warn: -50),
        ];
    }

    /**
     * Discover wireless SNR.  This is in dB. Type is snr.
     * Returns an array of LibreNMS\Device\Sensor objects that have been discovered
     *
     * @return array Sensors
     */
    public function discoverWirelessSnr()
    {
        $oid = '.1.3.6.1.4.1.31926.2.1.1.18.1'; // RADIO-BRIDGE-MIB::rfAverageCinr.1

        return [
            new WirelessSensor(WirelessSensorType::Snr, $this->getDeviceId(), $oid, 'siklu', 1, 'CINR'),
        ];
    }

    /**
     * Discover wireless air capacity. This is in bps. Type is rate.
     * Uses rfAirCapacity when the firmware supports it, otherwise calculates it from the current modulation profile.
     *
     * @return array Sensors
     */
    public function discoverWirelessRate()
    {
        $air_capacity = SnmpQuery::numeric()->get(self::AIR_CAPACITY_OID)->value();
        if (is_numeric($air_capacity) && $air_capacity > 0) {
            return [
                new WirelessSensor(WirelessSensorType::Rate, $this->getDeviceId(), self::AIR_CAPACITY_OID, 'siklu', 1, 'Air Capacity', null, 1000000),
            ];
        }

        $rate = $this->calculateAirCapacity(SnmpQuery::numeric()->get(self::MODE_OIDS)->values());
        if ($rate === null) {
            return [];
        }

        return [
            new WirelessSensor(WirelessSensorType::Rate, $this->getDeviceId(), self::MODE_OIDS, 'siklu-calculated', 1, 'Air Capacity', $rate),
        ];
    }

    public function pollWirelessRate(array $sensors)
    {
        $data = [];
        foreach ($sensors as $sensor) {
            // sensors using rfAirCapacity are polled normally
            if ($sensor['sensor_type'] !== 'siklu-calculated') {
                continue;
            }

            // always return a value, standard polling would sum the mode oids
            $data[$sensor['sensor_id']] = $this->calculateAirCapacity(SnmpQuery::numeric()->get(self::MODE_OIDS)->values());
        }

        return $data;
    }

    /**
     * @param  array  $values  MODE_OIDS => value
     * @return int|null capacity in bps
     */
    private function calculateAirCapacity(array $values): ?int
    {
        [$width, $modulation, $subchannels, $repetitions, $fec] = array_map(fn ($oid) => $values[$oid] ?? null, self::MODE_OIDS);
        if (! is_numeric($width) || ! is_numeric($modulation)) {
            return null;
        }

        $hardware = (string) $this->getDevice()->hardware;
        if (preg_match('/^EH-2[25]00F/', $hardware)) {
            $key = $modulation == 5 ? "$width:$modulation:$fec" : "$width:$modulation";
            $mbps = self::EH2200_CAPACITY[$key] ?? null;
        } elseif (preg_match('/^EH-(1200|600)T/', $hardware) && $fec == 1) {
            $mbps = self::EH1200_TDD_CAPACITY["$width:$modulation:$subchannels:$repetitions"] ?? null;
        } elseif (preg_match('/^EH-1200F/', $hardware) && $fec == 1) {
            $mbps = self::EH1200_FDD_CAPACITY["$width:$modulation:$subchannels:$repetitions"] ?? null;
        } else {
            $mbps = null;
        }

        return $mbps === null ? null : $mbps * 1000000;
    }
}
