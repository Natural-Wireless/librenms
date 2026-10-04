<?php

namespace LibreNMS\OS;

use LibreNMS\Device\WirelessSensor;
use LibreNMS\Enum\WirelessSensorType;
use LibreNMS\Interfaces\Discovery\Sensors\WirelessErrorsDiscovery;
use LibreNMS\Interfaces\Discovery\Sensors\WirelessPowerDiscovery;
use LibreNMS\Interfaces\Discovery\Sensors\WirelessRateDiscovery;
use LibreNMS\Interfaces\Discovery\Sensors\WirelessRssiDiscovery;
use LibreNMS\Interfaces\Discovery\Sensors\WirelessSnrDiscovery;
use LibreNMS\Interfaces\Polling\Sensors\WirelessRatePolling;
use LibreNMS\OS;
use SnmpQuery;

class HarmonyEnhanced extends OS implements WirelessRssiDiscovery, WirelessSnrDiscovery, WirelessPowerDiscovery, WirelessErrorsDiscovery, WirelessRateDiscovery, WirelessRatePolling
{
    public function discoverWirelessRssi()
    {
        $oids = snmpwalk_cache_oid($this->getDeviceArray(), 'mwrEmcRadioRSL', [], 'MWR-RADIO-MC-MIB', null, '-Ob');
        $sensors = [];
        foreach ($oids as $index => $entry) {
            $sensors[] = new WirelessSensor(
                WirelessSensorType::Rssi,
                $this->getDeviceId(),
                '.1.3.6.1.4.1.7262.4.5.12.203.1.1.5.' . $index,
                'harmony_enhanced',
                $index,
                'RSL Radio ' . $index,
                divisor: 10,
                low_limit: -60,
                low_warn: -50
            );
        }

        return $sensors;
    }

    public function discoverWirelessSnr()
    {
        $oids = snmpwalk_cache_oid($this->getDeviceArray(), 'mwrEmcRadioSNR', [], 'MWR-RADIO-MC-MIB', null, '-Ob');
        $sensors = [];
        foreach ($oids as $index => $entry) {
            $sensors[] = new WirelessSensor(
                WirelessSensorType::Snr,
                $this->getDeviceId(),
                '.1.3.6.1.4.1.7262.4.5.12.203.1.1.7.' . $index,
                'harmony_enhanced',
                $index,
                'SNR Radio ' . $index,
                divisor: 10
            );
        }

        return $sensors;
    }

    public function discoverWirelessPower()
    {
        $oids = snmpwalk_cache_oid($this->getDeviceArray(), 'mwrEmcRadioActualTxPower', [], 'MWR-RADIO-MC-MIB', null, '-Ob');
        $sensors = [];
        foreach ($oids as $index => $entry) {
            $sensors[] = new WirelessSensor(
                WirelessSensorType::Power,
                $this->getDeviceId(),
                '.1.3.6.1.4.1.7262.4.5.12.203.1.1.9.' . $index,
                'harmony_enhanced',
                $index,
                'TX Power Radio ' . $index,
                divisor: 10
            );
        }

        return $sensors;
    }

    public function discoverWirelessErrors()
    {
        $oids = snmpwalk_cache_oid($this->getDeviceArray(), 'mwrEmcRadioRxErrsFrames', [], 'MWR-RADIO-MC-MIB', null, '-Ob');
        $sensors = [];
        foreach ($oids as $index => $entry) {
            $sensors[] = new WirelessSensor(
                WirelessSensorType::Errors,
                $this->getDeviceId(),
                '.1.3.6.1.4.1.7262.4.5.12.203.1.1.4.' . $index,
                'harmony_enhanced',
                $index,
                'RX Errors Radio ' . $index
            );
        }

        return $sensors;
    }

    public function discoverWirelessRate()
    {
        $oids = snmpwalk_cache_oid($this->getDeviceArray(), 'mwrEmcRadioActualTxProfile', [], 'MWR-RADIO-MC-MIB', null, '-Ob');
        $sensors = [];
        foreach ($oids as $index => $entry) {
            $rate = $this->profileToRate($entry['mwrEmcRadioActualTxProfile'] ?? '');
            if ($rate === null) {
                continue;
            }

            $sensors[] = new WirelessSensor(
                WirelessSensorType::Rate,
                $this->getDeviceId(),
                '.1.3.6.1.4.1.7262.4.5.12.203.1.1.10.' . $index,
                'harmony_enhanced',
                $index,
                'TX Capacity Radio ' . $index,
                $rate
            );
        }

        return $sensors;
    }

    /**
     * The radio only reports the active TX profile name, so derive the rate from it
     */
    public function pollWirelessRate(array $sensors)
    {
        $oids = [];
        foreach ($sensors as $sensor) {
            $oids[$sensor['sensor_id']] = current($sensor['sensor_oids']);
        }

        $profiles = SnmpQuery::numeric()->get(array_values($oids))->values();

        $data = [];
        foreach ($oids as $sensor_id => $oid) {
            // always return a value, standard polling can't parse the profile name
            $data[$sensor_id] = $this->profileToRate($profiles[$oid] ?? '');
        }

        return $data;
    }

    /**
     * Profile names embed the capacity in Mbps, e.g. en50_454_2048qam
     */
    private function profileToRate(string $profile): ?int
    {
        if (preg_match('/^[a-z]+[\d.]+_(\d+)_/i', trim($profile, '" '), $matches)) {
            return (int) $matches[1] * 1000000;
        }

        return null;
    }
}
