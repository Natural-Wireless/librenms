<?php

// Cyclone battery pack voltage, one line per pack (device header graph for battery NPorts)
$graph_params->alt_y_grid = true;

$class = \LibreNMS\Enum\Sensor::Voltage;
$cyclone_key = 'voltage';

require 'includes/html/graphs/cyclone_battery.inc.php';
