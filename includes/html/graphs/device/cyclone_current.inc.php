<?php

// Cyclone battery current, one line per pack: + charging, - discharging (device header graph for battery NPorts)
$class = \LibreNMS\Enum\Sensor::Current;
$cyclone_key = 'current';

require 'includes/html/graphs/cyclone_battery.inc.php';
