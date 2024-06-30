<?php

namespace LibreNMS\OS;

use LibreNMS\Device\WirelessSensor;
use LibreNMS\Interfaces\Discovery\Sensors\WirelessPowerDiscovery;
use LibreNMS\Interfaces\Discovery\Sensors\WirelessRssiDiscovery;
use LibreNMS\OS;

class NecIpasolinkExAdvanced extends OS implements WirelessPowerDiscovery, WirelessRssiDiscovery
{
    public function discoverWirelessPower()
    {
        $oid = '.1.3.6.1.4.1.119.2.3.69.501.8.1.1.4.16842752';

        return [
            new WirelessSensor('power', $this->getDeviceId(), $oid, 'nec-ipasolink-ex-advanced', 0, 'Tx Power', null, 1, 1, "sum", null, 12, null, 8, null),
        ];
    }

    public function discoverWirelessRssi()
    {
        $oid = '.1.3.6.1.4.1.119.2.3.69.501.8.1.1.6.16842752';
        return [
            new WirelessSensor('rssi', $this->getDeviceId(), $oid, 'nec-ipasolink-ex-advanced', 0, 'RSL', null, 1, 1, "sum", null, null, -60, null, -50),
        ];
    }
}
