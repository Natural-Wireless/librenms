<?php

namespace LibreNMS\OS;

use LibreNMS\Device\WirelessSensor;
use LibreNMS\Enum\WirelessSensorType;
use LibreNMS\Interfaces\Discovery\Sensors\WirelessErrorsDiscovery;
use LibreNMS\Interfaces\Discovery\Sensors\WirelessPowerDiscovery;
use LibreNMS\Interfaces\Discovery\Sensors\WirelessRateDiscovery;
use LibreNMS\Interfaces\Discovery\Sensors\WirelessRssiDiscovery;
use LibreNMS\Interfaces\Discovery\Sensors\WirelessSnrDiscovery;
use LibreNMS\OS;

class HorizonCompactplus extends OS implements WirelessSnrDiscovery, WirelessPowerDiscovery, WirelessRateDiscovery, WirelessRssiDiscovery, WirelessErrorsDiscovery
{
    public function discoverWirelessSnr()
    {
        $oid = '.1.3.6.1.4.1.7262.2.5.4.2.1.1.8.1';

        return [
            new WirelessSensor(WirelessSensorType::Snr, $this->getDeviceId(), $oid, 'horizon-compactplus', 0, 'SNR', null, 1, 10),
        ];
    }

    public function discoverWirelessPower()
    {
        $oid = '.1.3.6.1.4.1.7262.2.5.4.4.1.1.7.1';

        return [
            new WirelessSensor(WirelessSensorType::Power, $this->getDeviceId(), $oid, 'horizon-compactplus', 0, 'Tx Power', null, 1, 10),
        ];
    }

    public function discoverWirelessRssi()
    {
        $oid = '.1.3.6.1.4.1.7262.2.5.4.2.1.1.3.1';

        return [
            new WirelessSensor(WirelessSensorType::Rssi, $this->getDeviceId(), $oid, 'horizon-compactplus', 0, 'RSL', null, 1, 10),
        ];
    }

    public function discoverWirelessErrors()
    {
        $oid = '.1.3.6.1.4.1.7262.2.5.4.2.2.1.4.1';

        return [
            new WirelessSensor(WirelessSensorType::Errors, $this->getDeviceId(), $oid, 'horizon-compactplus', 0, 'Rx Errors'),
        ];
    }

    public function discoverWirelessRate()
    {
        // Current modem speed estimate; divide by 10000 for Mbps, so multiply by 100 for bps
        return [
            new WirelessSensor(WirelessSensorType::Rate, $this->getDeviceId(), '.1.3.6.1.4.1.7262.2.5.4.2.1.1.7.1', 'horizon-compactplus-tx', 0, 'Tx Capacity', null, 100),
            new WirelessSensor(WirelessSensorType::Rate, $this->getDeviceId(), '.1.3.6.1.4.1.7262.2.5.4.2.1.1.6.1', 'horizon-compactplus-rx', 0, 'Rx Capacity', null, 100),
        ];
    }
}
