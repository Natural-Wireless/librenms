<?php

// Cyclone/PACE batteries on the NPort's serial port (opt-in per device, see LibreNMS\Device\CycloneBattery)
\LibreNMS\Device\CycloneBattery::discover($device, $sensor_class);
