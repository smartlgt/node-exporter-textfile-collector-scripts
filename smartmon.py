#!/usr/bin/env python3

import argparse
import collections
import csv
import datetime
import decimal
import json
import re
import shlex
import subprocess
import sys
from prometheus_client import CollectorRegistry, Gauge, generate_latest

device_info_re = re.compile(r'^(?P<k>[^:]+?)(?:(?:\sis|):)\s*(?P<v>.*)$')

ata_error_count_re = re.compile(
    r'^Error (\d+) \[\d+\] occurred', re.MULTILINE)

self_test_re = re.compile(r'^SMART.*(PASSED|OK)$', re.MULTILINE)

device_info_map = {
    'Vendor': 'vendor',
    'Product': 'product',
    'Revision': 'revision',
    'Logical Unit id': 'lun_id',
    'Model Family': 'model_family',
    'Device Model': 'device_model',
    'Serial Number': 'serial_number',
    'Serial number': 'serial_number',
    'Firmware Version': 'firmware_version',
}

smart_attributes_whitelist = (
    'airflow_temperature_cel',
    'command_timeout',
    'current_pending_sector',
    'end_to_end_error',
    'erase_fail_count_total',
    'g_sense_error_rate',
    'hardware_ecc_recovered',
    'host_reads_mib',
    'host_reads_32mib',
    'host_writes_mib',
    'host_writes_32mib',
    'load_cycle_count',
    'media_wearout_indicator',
    'wear_leveling_count',
    'nand_writes_1gib',
    'offline_uncorrectable',
    'power_cycle_count',
    'power_on_hours',
    'program_fail_count',
    'raw_read_error_rate',
    'reallocated_event_count',
    'reallocated_sector_ct',
    'reported_uncorrect',
    'sata_downshift_count',
    'seek_error_rate',
    'spin_retry_count',
    'spin_up_time',
    'start_stop_count',
    'temperature_case',
    'temperature_celsius',
    'temperature_internal',
    'total_lbas_read',
    'total_lbas_written',
    'udma_crc_error_count',
    'unsafe_shutdown_count',
    'workld_host_reads_perc',
    'workld_media_wear_indic',
    'workload_minutes',
)

registry = CollectorRegistry()
namespace = "smartmon"

metrics = {
    "smartctl_version": Gauge(
        "smartctl_version",
        "SMART metric smartctl_version",
        ["version"],
        namespace=namespace,
        registry=registry,
    ),
    "smartctl_run": Gauge(
        "smartctl_run",
        "SMART metric smartctl_run",
        ["device", "disk"],
        namespace=namespace,
        registry=registry,
    ),
    "device_active": Gauge(
        "device_active",
        "SMART metric device_active",
        ["device", "disk"],
        namespace=namespace,
        registry=registry,
    ),
    "device_info": Gauge(
        "device_info",
        "SMART metric device_info",
        [
            "device",
            "disk",
            "vendor",
            "product",
            "revision",
            "lun_id",
            "model_family",
            "device_model",
            "serial_number",
            "firmware_version",
        ],
        namespace=namespace,
        registry=registry,
    ),
    "device_smart_available": Gauge(
        "device_smart_available",
        "SMART metric device_smart_available",
        ["device", "disk"],
        namespace=namespace,
        registry=registry,
    ),
    "device_smart_enabled": Gauge(
        "device_smart_enabled",
        "SMART metric device_smart_enabled",
        ["device", "disk"],
        namespace=namespace,
        registry=registry,
    ),
    "device_smart_healthy": Gauge(
        "device_smart_healthy",
        "SMART metric device_smart_healthy",
        ["device", "disk"],
        namespace=namespace,
        registry=registry,
    ),

    # SMART attributes - ATA disks only
    "attr_value": Gauge(
        "attr_value",
        "SMART metric attr_value",
        ["device", "disk", "name"],
        namespace=namespace,
        registry=registry,
    ),
    "attr_worst": Gauge(
        "attr_worst",
        "SMART metric attr_worst",
        ["device", "disk", "name"],
        namespace=namespace,
        registry=registry,
    ),
    "attr_threshold": Gauge(
        "attr_threshold",
        "SMART metric attr_threshold",
        ["device", "disk", "name"],
        namespace=namespace,
        registry=registry,
    ),
    "attr_raw_value": Gauge(
        "attr_raw_value",
        "SMART metric attr_raw_value",
        ["device", "disk", "name"],
        namespace=namespace,
        registry=registry,
    ),
    "device_errors": Gauge(
        "device_errors",
        "SMART metric device_errors",
        ["device", "disk"],
        namespace=namespace,
        registry=registry,
    ),

    # json data
    "device_capabilities": Gauge(
        "device_capabilities",
        "SMART metric device_capabilities",
        ["device", "disk"],
        namespace=namespace,
        registry=registry,
    ),
    "device_data_collection": Gauge(
        "device_data_collection",
        "SMART metric device_data_collection",
        ["device", "disk", "status"],
        namespace=namespace,
        registry=registry,
    ),
    "device_power_on": Gauge(
        "device_power_on",
        "SMART metric device_power_on",
        ["device", "disk"],
        namespace=namespace,
        registry=registry,
    ),
    "device_self_test_latest": Gauge(
        "device_self_test_latest",
        "SMART metric device_self_test_latest",
        ["device", "disk"],
        namespace=namespace,
        registry=registry,
    ),
    "device_self_tests": Gauge(
        "device_self_tests",
        "SMART metric device_self_tests",
        ["device", "disk", "test", "passed", "entry"],
        namespace=namespace,
        registry=registry,
    ),

}

SmartAttribute = collections.namedtuple('SmartAttribute', [
    'id', 'name', 'flag', 'value', 'worst', 'threshold', 'type', 'updated',
    'when_failed', 'raw_value',
])


class Device(collections.namedtuple('DeviceBase', 'path opts')):
    """Representation of a device as found by smartctl --scan output."""

    @property
    def type(self):
        return self.opts.type

    @property
    def base_labels(self):
        return {'device': self.path, 'disk': self.type.partition('+')[2] or '0'}

    def smartctl_select(self):
        return ['--device', self.type, self.path]


def smart_ctl(*args, check=True):
    """Wrapper around invoking the smartctl binary.

    Returns:
        (str) Data piped to stdout by the smartctl subprocess.
    """
    p = subprocess.run(
        ['smartctl', *args], stdout=subprocess.PIPE
    )

    # check for specific allowed return codes
    # smartctl >= 7.0 has some (new) error sensitivity to errors
    # exit 4 - https://www.smartmontools.org/ticket/1233
    # exit 64 (error in the past) - https://bugs.launchpad.net/maas/+bug/1783889
    allowed_return_codes = [0, 4, 64]
    if check and p.returncode not in allowed_return_codes:
        raise subprocess.CalledProcessError(p.returncode, p.args,
                                            output=p.stdout, stderr=p.stderr)
    return p.stdout.decode('utf-8')


def smart_ctl_json(device):
    json_output = smart_ctl('-j', '-a', *device.smartctl_select())
    return json.loads(json_output)


def smart_ctl_version():
    return smart_ctl('-V').split('\n')[0].split()[1]


def find_devices(by_id):
    """Find SMART devices.

    Yields:
        (Device) Single device found by smartctl.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', '--device', dest='type')

    args = ['--scan-open']
    if by_id:
        args.extend(['-d', 'by-id'])
    devices = smart_ctl(*args)

    for device in devices.split('\n'):
        device = device.strip()
        if not device:
            continue

        tokens = shlex.split(device, comments=True)
        if not tokens:
            continue

        # check for virtual devices
        p = parser.parse_args(tokens[1:])
        info = smart_ctl('--nocheck', 'standby', '--info', '--device', p.type, tokens[0])
        rows = info.strip().split('\n')[3:]
        found = False
        for r in rows:
            if r.lower().startswith("product") and "VIRTUAL-DISK".lower() in r.lower():
                # skip virtual device from longhorn
                found = True
                break
        if found:
            continue

        yield Device(tokens[0], p)


def device_is_active(device):
    """Returns whenever the given device is currently active or not.

    Args:
        device: (Device) Device in question.

    Returns:
        (bool) True if the device is active and False otherwise.
    """
    try:
        smart_ctl('--nocheck', 'standby', *device.smartctl_select())
    except subprocess.CalledProcessError:
        return False

    return True


def device_info(device):
    """Query device for basic model information.

    Args:
        device: (Device) Device in question.

    Returns:
        (generator): Generator yielding:

            key (str): Key describing the value.
            value (str): Actual value.
    """
    info_lines = smart_ctl(
        '--info', *device.smartctl_select()
    ).strip().split('\n')[3:]

    matches = (device_info_re.match(line) for line in info_lines)
    return (m.groups() for m in matches if m is not None)


def device_smart_capabilities(device):
    """Returns SMART capabilities of the given device.

    Args:
        device: (Device) Device in question.

    Returns:
        (tuple): tuple containing:

            (bool): True whenever SMART is available, False otherwise.
            (bool): True whenever SMART is enabled, False otherwise.
    """
    groups = device_info(device)

    state = {
        g[1].split(' ', 1)[0]
        for g in groups if g[0] == 'SMART support'}

    smart_available = 'Available' in state
    smart_enabled = 'Enabled' in state

    return smart_available, smart_enabled


def collect_device_info(device):
    """Collect basic device information.

    Args:
        device: (Device) Device in question.
    """
    values = dict(device_info(device))
    metrics["device_info"].labels(
        device.base_labels["device"],
        device.base_labels["disk"],
        values.get("Vendor", ""),
        values.get("Product", ""),
        values.get("Revision", ""),
        values.get("Logical Unit id", ""),
        values.get("Model Family", ""),
        values.get("Device Model", ""),
        values.get("Serial Number", ""),
        values.get("Firmware Version", ""),
    ).set(1)


def collect_device_health_self_assessment(device):
    """Collect metric about the device health self assessment.

    Args:
        device: (Device) Device in question.
    """
    out = smart_ctl('--health', *device.smartctl_select(), check=False)

    self_assessment_passed = bool(self_test_re.search(out))
    metrics["device_smart_healthy"].labels(
        device.base_labels["device"], device.base_labels["disk"]
    ).set(self_assessment_passed)


def collect_ata_metrics(device):
    # Fetch SMART attributes for the given device.
    attributes = smart_ctl(
        '--attributes', *device.smartctl_select()
    )

    # replace multiple occurrences of whitespace with a single whitespace
    # so that the CSV Parser recognizes individual columns properly.
    attributes = re.sub(r'[\t\x20]+', ' ', attributes)

    # Turn smartctl output into a list of lines and skip to the table of
    # SMART attributes.
    attribute_lines = attributes.strip().split('\n')[7:]

    # Some attributes have multiple IDs but have the same name.  Don't
    # yield attributes that already have been reported before.
    seen = set()

    reader = csv.DictReader(
        (line.strip() for line in attribute_lines),
        fieldnames=SmartAttribute._fields[:-1],
        restkey=SmartAttribute._fields[-1], delimiter=' ')
    for entry in reader:
        # We're only interested in the SMART attributes that are
        # whitelisted here.
        entry['name'] = entry['name'].lower()
        if entry['name'] not in smart_attributes_whitelist:
            continue

        # Ensure that only the numeric parts are fetched from the raw_value.
        # Attributes such as 194 Temperature_Celsius reported by my SSD
        # are in the format of "36 (Min/Max 24/40)" which can't be expressed
        # properly as a prometheus metric.
        m = re.match(r'^(\d+)', ' '.join(entry['raw_value']))
        if not m:
            continue
        entry['raw_value'] = m.group(1)

        # Some device models report "---" in the threshold value where most
        # devices would report "000". We do the substitution here because
        # downstream code expects values to be convertable to integer.
        if entry['threshold'] == '---':
            entry['threshold'] = '0'

        if entry['name'] in smart_attributes_whitelist and entry['name'] not in seen:
            for col in 'value', 'worst', 'threshold', 'raw_value':
                metrics["attr_" + col].labels(
                    device.base_labels["device"],
                    device.base_labels["disk"],
                    entry["name"],
                ).set(entry[col])

            seen.add(entry['name'])


def collect_ata_error_count(device):
    """Inspect the device error log and report the amount of entries.

    Args:
        device: (Device) Device in question.
    """
    error_log = smart_ctl(
        '-l', 'xerror,1', *device.smartctl_select(), check=False)

    m = ata_error_count_re.search(error_log)

    error_count = m.group(1) if m is not None else 0
    metrics["device_errors"].labels(
        device.base_labels["device"], device.base_labels["disk"]
    ).set(error_count)


def collect_disks_metrics_json(wakeup_disks, by_id, data_collection=True, self_test_latest=True, capabilities=True,
                               self_test_log=True):
    # ensure labels get reset for the following metrics
    metrics['device_self_test_latest'].clear()
    metrics['device_data_collection'].clear()
    metrics['device_self_tests'].clear()

    for device in find_devices(by_id):
        is_active = device_is_active(device)

        # Skip further metrics collection to prevent the disk from
        # spinning up.
        if not is_active and not wakeup_disks:
            continue

        # collect test results for the device
        data = smart_ctl_json(device)

        collect_power_on(device, data)

        if self_test_log:
            collect_self_test_logs(device, data)

        if data_collection:
            collect_offline_data_collection(device, data)

        if self_test_latest:
            collect_self_test_latest(device, data)

        if capabilities:
            collect_capabilities(device, data)


def collect_offline_data_collection(device, data):
    # offline_data_collection
    device_label = device.base_labels
    if data.get("ata_smart_data", None) and data["ata_smart_data"].get("offline_data_collection", None):
        device_label["status"] = str(data["ata_smart_data"]["offline_data_collection"]["status"]["value"])
        metrics['device_data_collection'].labels(
            **device_label
        ).set(data["ata_smart_data"]["offline_data_collection"]["completion_seconds"])


def collect_self_test_latest(device, data):
    # self_test (latest/running)
    device_label = device.base_labels
    if data.get("ata_smart_data", None) and data["ata_smart_data"].get("self_test", None):
        device_label["status"] = str(data["ata_smart_data"]["self_test"]["status"]["value"])
        percentage = data["ata_smart_data"]["self_test"]["status"].get("remaining_percent", 0)
        metrics['device_self_test_latest'].labels(
            device.base_labels["device"], device.base_labels["disk"]
        ).set(percentage)


def collect_capabilities(device, data):
    # capabilities
    device_label = device.base_labels
    if data.get("ata_smart_data", None) and data["ata_smart_data"].get("capabilities", None):
        for cap, value in data["ata_smart_data"]["capabilities"].items():
            if cap != "values":
                device_label[cap] = str(value).lower()
        metrics['device_capabilities'].labels(
            **device_label
        ).set(1)


def collect_power_on(device, data):
    # extra timing info, tests with no time reference are not useful
    if data.get('power_on_time', None) and data["power_on_time"].get('hours', None):
        power_on_time = data["power_on_time"]["hours"]
        metrics['device_power_on'].labels(
            device.base_labels["device"], device.base_labels["disk"]
        ).set(power_on_time)


def collect_self_test_logs(device, data):
    tests = data.get('ata_smart_self_test_log', None)
    if tests and 'standard' in tests:
        table = tests.get('standard').get('table', None)
        i = 0
        for entry in reversed(table):
            i += 1
            power_on_hours = entry['lifetime_hours']

            device_label = device.base_labels
            tests_value_map = {1: "short", 2: "long", 129: "short_captive", 255: "vendor"}
            found = False
            for value in tests_value_map.keys():
                if value == entry['type']['value']:
                    device_label['test'] = tests_value_map[value]
                    found = True
                    break

            if not found:
                device_label['test'] = "unknown"

            # check if the test finished
            if entry['status']['value'] != 0:
                # skip running tests
                continue

            # metric_print cant hale boolean values, force to string output
            device_label["passed"] = str(entry['status']['passed']).lower()

            device_label["entry"] = str(i)

            metrics['device_self_tests'].labels(
                **device_label
            ).set(power_on_hours)


def collect_disks_smart_metrics(wakeup_disks, by_id):
    now = int(datetime.datetime.utcnow().timestamp())

    for device in find_devices(by_id):
        metrics['smartctl_run'].labels(
            device.base_labels["device"], device.base_labels["disk"]
        ).set(now)
        is_active = device_is_active(device)
        metrics["device_active"].labels(
            device.base_labels["device"], device.base_labels["disk"],
        ).set(is_active)

        # Skip further metrics collection to prevent the disk from spinning up.
        if not is_active and not wakeup_disks:
            continue

        collect_device_info(device)

        smart_available, smart_enabled = device_smart_capabilities(device)

        metrics["device_smart_available"].labels(
            device.base_labels["device"], device.base_labels["disk"]
        ).set(smart_available)

        metrics["device_smart_enabled"].labels(
            device.base_labels["device"], device.base_labels["disk"]
        ).set(smart_enabled)

        # Skip further metrics collection here if SMART is disabled on the device. Further smartctl
        # invocations would fail anyway.
        if not smart_available:
            continue

        collect_device_health_self_assessment(device)

        if device.type.startswith('sat'):
            collect_ata_metrics(device)
            collect_ata_error_count(device)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-s', '--wakeup-disks', dest='wakeup_disks', action='store_true',
                        help="Wake up disks to collect live stats")
    parser.add_argument('--by-id', dest='by_id', action='store_true',
                        help="Use /dev/disk/by-id/X instead of /dev/sdX to index devices")
    args = parser.parse_args(sys.argv[1:])

    metrics["smartctl_version"].labels(smart_ctl_version()).set(1)

    collect_disks_smart_metrics(args.wakeup_disks, args.by_id)

    # new metrics form json output
    try:
        if float(smart_ctl_version()) >= 7.0:
            collect_disks_metrics_json(args.wakeup_disks, args.by_id)
    except ValueError:
        pass

    print(generate_latest(registry).decode(), end="")


if __name__ == '__main__':
    main()
