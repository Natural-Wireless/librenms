<?php

// One line per Cyclone battery pack for a single reading ($cyclone_key: soc, voltage, current), see
// LibreNMS\Device\CycloneBattery. Like device/sensor.inc.php, minus the cell and SOH sensors of the
// same class that would flatten the line in the small device header graphs.

use App\Facades\Rrd;
use App\Models\Sensor;
use LibreNMS\Device\CycloneBattery;
use LibreNMS\Exceptions\RrdGraphException;

require 'includes/html/graphs/common.inc.php';

$sensors = Sensor::where('sensor_class', $class->value)
    ->where('device_id', $device['device_id'])
    ->where('sensor_type', CycloneBattery::TYPE_PREFIX)
    ->get()
    ->filter(fn (Sensor $sensor) => preg_match('/^cyclone\.\d+\.' . preg_quote($cyclone_key, '/') . '$/', $sensor->sensor_index))
    ->sortBy('sensor_index', SORT_NATURAL)
    ->values();

if ($sensors->isEmpty()) {
    throw new RrdGraphException('No Battery');
}

$unit_short = str_replace('%', '%%', $class->unit());
$unit_long = str_replace('%', '%%', $class->unitLong());

$col_w = 8 + strlen($unit_short);
$rrd_options[] = 'COMMENT:' . str_pad($unit_long, 12) . str_pad('Cur', $col_w) . str_pad('Min', $col_w) . str_pad('Max', $col_w) . 'Avg\\n';

foreach ($sensors as $index => $sensor) {
    $colour = ['CC0000', '008C00', '4096EE', '73880A', 'D01F3C', '36393D', 'FF0084'][$index % 7];
    $field = 'sensor' . $sensor->sensor_id;
    $rrd_filename = Rrd::name($device['hostname'], get_sensor_rrd_name($device, $sensor));

    $rrd_options[] = "DEF:$field=$rrd_filename:sensor:AVERAGE";
    $rrd_options[] = "LINE1.5:$field#$colour:" . str_pad('Pack ' . explode('.', $sensor->sensor_index)[1], 10);
    $rrd_options[] = "GPRINT:$field:LAST:%6.2lf$unit_short";
    $rrd_options[] = "GPRINT:$field:MIN:%6.2lf$unit_short";
    $rrd_options[] = "GPRINT:$field:MAX:%6.2lf$unit_short";
    $rrd_options[] = "GPRINT:$field:AVERAGE:%6.2lf$unit_short\\l";
}
