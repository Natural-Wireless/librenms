<?php

// Cyclone battery state of charge, one line per pack (device header graph for battery NPorts)
$class = \LibreNMS\Enum\Sensor::Charge;
$cyclone_key = 'soc';

require 'includes/html/graphs/cyclone_battery.inc.php';
