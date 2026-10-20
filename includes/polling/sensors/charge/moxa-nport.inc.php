<?php

// Battery sensors are read through the NPort's serial port, not SNMP (see LibreNMS\Device\CycloneBattery)
$sensor_value = \LibreNMS\Device\CycloneBattery::pollValue($device, $sensor, $sensor_value);
