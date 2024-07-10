#!/usr/bin/env python3
# coding: utf-8

"""This script manages patching Rocky 9 Linux VMs on Proxmox
It checks the status of all virtual machines, saves all VMs that are stopped,
starts the stopped VMs, checks for SSH availability, creates a snapshot and
then patches the VMs.
After patching all initially stopped VMs will be shut
down again.
After the patching is done, a Telegram notification is sent.
To configure the script, the following environment variables have to be set:

PROXMOX_HOST        = FQDN of your Proxmox host
PROXMOX_USER        = The user which is used to connect to the Proxmox API
PROXMOX_PASSWORD    = The password for the user
PROXMOX_VERIFY_SSL  = True or false. Defaults to false if not set
SSH_USER            = The user used to connect to the VMs
SSH_KEY_FILE        = The path to the SSH key file used for authentication
SSH_TIMEOUT         = Timeout of SSH login. Defaults to 300 seconds if not set
SSH_RETRY_INTERVAL  = Retry interval for the SSH availability check. Defaults to 10 seconds
ENABLE_NOTIFICATION = Enables notification message via Telegram. Defaults to true
TELEGRAM_BOT_TOKEN  = The authentication token of your Telegram bot
TELEGRAM_CHAT_ID    = The channel identifier to send the message to
POST_REQ_TIMEOUT    = Timeout for post requests. Defaults to 30 seconds
ENABLE_PATCH_OUTPUT = Prints stdout of update command in pipeline
DOMAIN              = Your domain
                      Is used as a fallback, if qemu-guest-agent can't get the hostname
"""


import os
import time
import paramiko
import requests
from proxmoxer import ProxmoxAPI

# Proxmox configuration
PROXMOX_HOST = os.getenv("PROXMOX_HOST")
PROXMOX_USER = os.getenv("PROXMOX_USER")
PROXMOX_PASSWORD = os.getenv("PROXMOX_PASSWORD")
PROXMOX_VERIFY_SSL = os.getenv("PROXMOX_VERIFY_SSL", "false").lower() in (
    "true",
    "1",
    "t",
    "on",
    "yes",
    "y",
)

# SSH configuration
SSH_USER = os.getenv("SSH_USER")
SSH_KEY_FILE = os.getenv("SSH_KEY_FILE")
SSH_TIMEOUT = int(os.getenv("SSH_TIMEOUT", "300"))  # Default to 300 seconds
SSH_RETRY_INTERVAL = int(os.getenv("SSH_RETRY_INTERVAL", "10"))  # Default to 10 seconds

# Telegram Configuration
ENABLE_NOTIFICATION = os.getenv("ENABLE_NOTIFICATION", "True")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Request configuration
POST_REQ_TIMEOUT = int(os.getenv("POST_REQ_TIMEOUT", "30"))  # Default to 30 seconds

# Trigger to enable patch output
ENABLE_PATCH_OUTPUT = os.getenv("ENABLE_PATCH_OUTPUT", "False")

# Domain
DOMAIN = os.getenv("DOMAIN")

# Connect to proxmox
proxmox = ProxmoxAPI(
    PROXMOX_HOST,
    user=PROXMOX_USER,
    password=PROXMOX_PASSWORD,
    verify_ssl=PROXMOX_VERIFY_SSL,
)
node = proxmox.nodes.get()[0]["node"]

# Patchmanagement statistics
stats = {
    "failed_snapshots": [],
    "patched_vms": [],
    "failed_patches": [],
    "ssh_failed_vms": [],
    "needs_reboot": [],
    "manual_patches": [],
    "unsupported": [],
}


class Style:
    """ANSI color codes for output styling"""

    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    BLUE = "\033[0;34m"
    YELLOW = "\033[0;33m"
    PURPLE = "\033[0;35m"
    CYAN = "\033[0;36m"
    NC = "\033[0m"


def check_requirements():
    """Checks if all requirements are satisfied"""
    if convert_to_bool(ENABLE_NOTIFICATION):
        return all(
            v is not None
            for v in [
                DOMAIN,
                PROXMOX_HOST,
                PROXMOX_USER,
                PROXMOX_PASSWORD,
                SSH_USER,
                SSH_KEY_FILE,
                TELEGRAM_BOT_TOKEN,
                TELEGRAM_CHAT_ID,
            ]
        )
    return all(
        v is not None
        for v in [
            DOMAIN,
            PROXMOX_HOST,
            PROXMOX_USER,
            PROXMOX_PASSWORD,
            SSH_USER,
            SSH_KEY_FILE,
        ]
    )


def convert_to_bool(value):
    """Convers a given value to a boolean based on known boolean representations"""
    value = value.lower()
    return value in ("y", "yes", "t", "true", "on", "1")


def update_stats(stat, host, pkg=None):
    """Updates a single statistic in the stats map"""
    data = stats[stat]
    if pkg is not None:
        data.append((host, pkg))
    else:
        data.append(host)
    stats[stat] = data


def start_vm(vmid):
    """Starts a VM via the proxmox API"""
    print(f"Starting VM: {Style.BLUE}{vmid}{Style.NC}")
    proxmox.nodes(node).qemu(vmid).status().start.post(timeout=POST_REQ_TIMEOUT)


def stop_vm(vmid):
    """Stops a VM via the proxmox API"""
    print(f"Stopping VM: {Style.BLUE}{vmid}{Style.NC}")
    proxmox.nodes(node).qemu(vmid).status().shutdown.post(timeout=POST_REQ_TIMEOUT)


def reboot_vm(vmid):
    """Reboots a VM via the proxmox API"""
    proxmox.nodes(node).qemu(vmid).status().reboot.post(timeout=POST_REQ_TIMEOUT)


def get_hostname(vm):
    """Gets the hostname for a VM.
    Falls back to VM name and domain if no guest agent is enabled
    """
    config = proxmox.nodes(node).qemu(vm["vmid"]).config.get()
    if config["agent"] == 1:
        data = proxmox.nodes(node).qemu(vm["vmid"]).agent.get("get-host-name")
        host = data["result"]["host-name"]
    else:
        host = f"{vm["name"]}.{DOMAIN}"
    return host


def get_vms():
    """Gets the status of all VMs, their hostname, VM ID and the tags
    Patching and rebooting is controlled via the tags.
    If a predefined reboot and patch tag are found, the values in the map are set accordingly.
    """
    all_vms = proxmox.nodes(node).qemu.get()
    vms = {}
    for vm in all_vms:
        if vm.get("template", 0) == 1:
            continue
        if "patch" not in vm.get("tags", []).split(";"):
            update_stats("manual_patches", f"{vm["name"]}.{DOMAIN}")
            continue
        hostname = get_hostname(vm)
        reboot = False
        if "reboot" in vm.get("tags", []).split(";"):
            reboot = True
        vms[vm["vmid"]] = {
            "hostname": hostname,
            "status": vm["status"],
            "reboot": reboot,
        }
    return vms


def ssh_command(host, command):
    """Executes a given command on a host via SSH"""
    print(f"Connecting to Host: {Style.BLUE}{host}{Style.NC}")
    print(f"Executing command: {Style.PURPLE}{command}{Style.NC}")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(hostname=host, username=SSH_USER, key_filename=SSH_KEY_FILE)
    stdin, stdout, stderr = ssh.exec_command(command)
    exit_status = stdout.channel.recv_exit_status()
    ssh.close()
    return exit_status, stdout.read().decode(), stderr.read().decode()


def ssh_available(host):
    """Checks if SSH is available for a given host"""
    start_time = time.time()
    total_attempts = SSH_TIMEOUT // SSH_RETRY_INTERVAL
    attempt = 0
    while time.time() - start_time < SSH_TIMEOUT:
        attempt = attempt + 1
        try:
            if total_attempts // 5 * 4 < attempt:
                style = Style.RED
            elif total_attempts // 2 < attempt:
                style = Style.YELLOW
            else:
                style = Style.GREEN
            print(
                f"Attempting connection to {Style.BLUE}{host}{Style.NC}. "
                f"{style}{attempt}/{total_attempts} attempts.{Style.NC}"
            )
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(hostname=host, username=SSH_USER, key_filename=SSH_KEY_FILE)
            ssh.close()
            return True
        except (
            paramiko.ssh_exception.NoValidConnectionsError,
            paramiko.ssh_exception.SSHException,
        ):
            print(
                f"{Style.YELLOW}Connection attempt to {Style.BLUE}{host}{Style.YELLOW} failed."
                f"{Style.NC} Waiting {SSH_RETRY_INTERVAL} seconds to retry."
            )
            time.sleep(SSH_RETRY_INTERVAL)
    return False


def set_update_command(host):
    """Detects the package manager to use and sets the according update command"""
    command = "which dnf"
    exit_status, stdout, stderr = ssh_command(host, command)
    if stdout:
        lines = stdout.splitlines()
        if lines[0].startswith("/") and lines[0].endswith("dnf"):
            print(
                f"{Style.GREEN}Red Hat based distro detected.{Style.NC} "
                f"Setting package manager to {Style.PURPLE}{lines[0]}{Style.NC}"
            )
            distro = "redhat"
            package_manager = lines[0]
            update_command = "update -y"
            return distro, package_manager, update_command
    command = "which apt-get"
    exit_status, stdout, stderr = ssh_command(host, command)
    if stdout:
        lines = stdout.splitlines()
        if lines[0].startswith("/") and lines[0].endswith("apt-get"):
            print(
                f"{Style.GREEN}Debian based distro detected.{Style.NC} "
                f"Setting package manager to {Style.PURPLE}{lines[0]}{Style.NC}"
            )
            distro = "debian"
            package_manager = lines[0]
            update_command = "upgrade -y"
            return distro, package_manager, update_command
    print(
        f"{Style.RED}Unsupported distro detected! "
        f"Could not set package manager!{Style.NC}"
    )
    return None, None, None


def count_updated_packages(output, distro):
    """Counts the amount of packages which were updated"""
    lines = output.splitlines()
    if distro == "redhat":
        in_upgrade_section = False
        updated_packages = 0
        for line in lines:
            if line.startswith("Upgraded:"):
                in_upgrade_section = True
            elif (
                in_upgrade_section
                and line.startswith("Installed:")
                or line.strip() == ""
            ):
                continue
            elif in_upgrade_section and (
                line.startswith("Removed:") or line.startswith("Complete!")
            ):
                break
            elif in_upgrade_section:
                updated_packages += 1
    elif distro == "debian":
        updated_packages = 0
        for line in lines:
            if "upgraded" not in line:
                continue
            updated_packages = line.split(" ", 1)[0]
    return updated_packages


def send_telegram_message(message):
    """Function to send a Telegram notification."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    response = requests.post(url, data=data, timeout=POST_REQ_TIMEOUT)
    if response.status_code != 200:
        print(
            f"{Style.RED}Failed to send message:{Style.NC} {response.status_code}, {response.text}"
        )


def delete_latest_snapshot(vmid):
    """Function to get and delete the latest snapshot"""
    snapshots = proxmox.nodes(node).qemu(vmid).snapshot.get()
    if snapshots and len(snapshots) > 1:
        latest_snapshot = snapshots[-2]["name"]
        print(
            f"Snapshot {Style.CYAN}{latest_snapshot}{Style.NC} found. Deleting snapshot..."
        )
        proxmox.nodes(node).qemu(vmid).snapshot(latest_snapshot).delete()
        time.sleep(5)
    else:
        print("No snapshot found. Continue as normal...")


def create_snapshot(vmid):
    """Function to create a snapshot"""
    snapshot_name = f"snapshot-{str(time.time_ns())}"
    print(f"Creating new snapshot: {Style.CYAN}{snapshot_name}{Style.NC}")
    proxmox.nodes(node).qemu(vmid).snapshot.post(snapname=snapshot_name)
    time.sleep(5)
    return snapshot_name


def verify_snapshot(vmid, host, snapshot_name):
    """Function to verify the existance of a snapshot"""
    existing_snapshots = proxmox.nodes(node).qemu(vmid).snapshot.get()
    if any(snapshot["name"] == snapshot_name for snapshot in existing_snapshots):
        return True
    print(
        f"{Style.RED}Snapshot {Style.NC}{snapshot_name}{Style.RED} was not found for "
        f"{Style.BLUE}{host}{Style.NC}"
    )
    update_stats("failed_snapshots", host)
    return False


def manage_snapshots(vmid, host):
    """Function to manage snapshots.
    This will delete the latest snapsot, create a new one and verify it
    """
    delete_latest_snapshot(vmid)
    try:
        snapshot_name = create_snapshot(vmid)
        return verify_snapshot(vmid, host, snapshot_name)
    except Exception as e:
        print(
            f"{Style.RED}Snapshot creation failed for {Style.BLUE}{host}{Style.NC}: {e}"
        )
        update_stats("failed_snapshots", host)
    return False


def message_section(header, style, content):
    """Function to generate a message section"""
    section_lines = []
    section_lines.append(header)
    print(f"\n\n{style}{header}{Style.NC}\n")
    for item in content:
        section_lines.append(item)
        print(f"{Style.BLUE}{item}{Style.NC}")
    return section_lines


def result_header(failed_vms, failed_snapshots, failed_patches):
    """Function to determine the header of the result output"""
    if failed_vms or failed_snapshots or failed_patches:
        header = "Patchmanagement completed with errors\n"
        print(f"\n\n{Style.RED}Patchmanagement completed with errors{Style.NC}\n")
    else:
        header = "Patchmanagement completed successfully\n"
        print(f"\n\n{Style.GREEN}Patchmanagement completed successfully{Style.NC}\n")
    return header


def patched_vms(vms):
    """Part of the result output section"""
    section_lines = []
    section_lines.append("The following VMs have been patched:")
    print("\n\nThe following VMs have been patched:\n")
    for host, updated_packages in vms:
        section_lines.append(f"{host}: {updated_packages} packages updated")
        print(f"{Style.BLUE}{host}:{Style.NC} {updated_packages} packages updated")
    return section_lines


def generate_notification(statistics):
    """Generate a notification message."""
    message_lines = []
    message_lines.append(
        result_header(
            statistics["ssh_failed_vms"],
            statistics["failed_snapshots"],
            statistics["failed_patches"],
        )
    )
    message_lines = message_lines + patched_vms(statistics["patched_vms"])
    if statistics["failed_patches"]:
        message_lines = message_lines + message_section(
            "Failed to patch the following VMs:",
            Style.RED,
            statistics["failed_patches"],
        )
    if statistics["ssh_failed_vms"]:
        message_lines = message_lines + message_section(
            "Failed to connect to the following VMs:",
            Style.RED,
            statistics["ssh_failed_vms"],
        )
    if statistics["failed_snapshots"]:
        message_lines = message_lines + message_section(
            "Failed to create snapshots for the following VMs:",
            Style.RED,
            statistics["failed_snapshots"],
        )
    if statistics["needs_reboot"]:
        message_lines = message_lines + message_section(
            "The following VMs have to be rebooted manually:",
            Style.RED,
            statistics["needs_reboot"],
        )
    if statistics["manual_patches"]:
        message_lines = message_lines + message_section(
            "The following VMs are configured to be manually patched:",
            Style.YELLOW,
            statistics["manual_patches"],
        )
    if statistics["unsupported"]:
        message_lines = message_lines + message_section(
            "The following VMs are unsupported and could not be patched:",
            Style.RED,
            statistics["unsupported"],
        )
    return "\n".join(message_lines)


def patch_host(host, distro, package_manager, update_command):
    """Function to patch a host"""
    print(f"Starting patch for {Style.BLUE}{host}{Style.NC}...")
    if distro == "debian":
        command = f"sudo {package_manager} update -y"
        exit_status, stdout, stderr = ssh_command(host, command)
        if exit_status != 0:
            print(
                f"{Style.RED}Error while updating the package database! "
                f"Skipping {Style.BLUE}{host}{Style.NC}"
            )
            update_stats("failed_patches", host)
            return False
    command = f"sudo {package_manager} {update_command}"
    exit_status, stdout, stderr = ssh_command(host, command)
    if stdout:
        if convert_to_bool(ENABLE_PATCH_OUTPUT):
            print(f"Output from {Style.BLUE}{host}{Style.NC}:\n{stdout}")
        updated_packages = count_updated_packages(stdout, distro)
        print(
            f"{Style.GREEN}{updated_packages} packages updated on {Style.BLUE}{host}{Style.NC}"
        )
        update_stats("patched_vms", host, updated_packages)
        return True
    if stderr:
        print(f"Errors from {host}:\n{stderr}")
        update_stats("failed_patches", host)
        return False
    update_stats("failed_patches", host)
    return False


def reboot_host(vmid, vm, distro):
    """Checks if a VM needs a reboot and restarts it"""
    print(
        f"Checking if reboot for {Style.BLUE}{vm["hostname"]}{Style.NC} is necessary..."
    )
    reboot_required = False
    if distro == "debian":
        command = "sudo ls -lah /var/run/reboot-required"
        exit_status, stdout, stderr = ssh_command(vm["hostname"], command)
        if exit_status == 0:
            reboot_required = True
    elif distro == "redhat":
        command = "sudo needs-restarting -r"
        exit_status, stdout, stderr = ssh_command(vm["hostname"], command)
        if exit_status == 1:
            reboot_required = True
    if reboot_required:
        if vm["reboot"]:
            print(
                f"{Style.YELLOW}Reboot required on {Style.BLUE}{vm["hostname"]}{Style.NC}. "
                f"Rebooting now..."
            )
            reboot_vm(vmid)
        else:
            update_stats("needs_reboot", vm["hostname"])
    else:
        print(
            f"{Style.GREEN}No reboot required on {Style.BLUE}{vm["hostname"]}{Style.NC}."
        )


def patch_vm(vm, vmid):
    """Patch a VM."""
    print(f"Waiting for SSH to become available on {vm["hostname"]}...")
    if ssh_available(vm["hostname"]):
        print(
            f"{Style.GREEN}SSH is available on {Style.BLUE}{vm["hostname"]}{Style.NC}. "
            f"Looking for snapshot..."
        )
        if manage_snapshots(vmid, vm["hostname"]):
            print(f"{Style.GREEN}Snapshot successfully created.{Style.NC}")
        else:
            print(
                f"{Style.RED}Skipping patch for {Style.BLUE}{vm["hostname"]}{Style.RED}!{Style.NC}"
            )
            return False
        distro, package_manager, update_command = set_update_command(vm["hostname"])
        if distro is None:
            print(
                f"{Style.RED}Skipping patch for {Style.BLUE}{vm["hostname"]}{Style.NC}"
            )
            update_stats("unsupported", vm["hostname"])
            return False
        if patch_host(vm["hostname"], distro, package_manager, update_command):
            reboot_host(vmid, vm, distro)
            return True
        return False
    print(
        f"{Style.RED}SSH not available on {Style.BLUE}{vm["hostname"]}{Style.RED} "
        f"after {SSH_TIMEOUT} seconds!{Style.NC} Skipping patch."
    )
    update_stats("ssh_failed_vms", vm["hostname"])
    return False


def main():
    """Main function to patch all VMs."""
    check_requirements()
    vms = get_vms()
    initially_stopped_vms = []
    for vmid, vm in vms.items():
        if vm["status"] == "stopped":
            start_vm(vmid)
            initially_stopped_vms.append(vmid)
        if patch_vm(vm, vmid):
            print(
                f"{Style.GREEN}Patching of {Style.BLUE}{vm["hostname"]} "
                f"{Style.GREEN}complete{Style.NC}."
            )
        else:
            print(
                f"{Style.RED}Patching of {Style.BLUE}{vm["hostname"]} "
                f"{Style.RED}failed!{Style.NC}"
            )
    for vmid in initially_stopped_vms:
        stop_vm(vmid)
    if convert_to_bool(ENABLE_NOTIFICATION):
        message = generate_notification(stats)
        send_telegram_message(message)


if __name__ == "__main__":
    main()
