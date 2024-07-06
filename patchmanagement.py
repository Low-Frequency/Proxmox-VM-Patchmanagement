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

INVENTORY_FILE      = Path to your inventory file
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
DOMAIN              = Your domain. Is used to create the FQDN of the hosts to patch.
"""


import os
import time
import yaml
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
    "y"
)

# Inventory file with VMs to patch
INVENTORY_FILE = os.getenv("INVENTORY_FILE", "inventory.yml")

# SSH configuration
SSH_USER = os.getenv("SSH_USER")
SSH_KEY_FILE = os.getenv("SSH_KEY_FILE")
SSH_TIMEOUT = int(os.getenv("SSH_TIMEOUT", "300"))
SSH_RETRY_INTERVAL = int(os.getenv("SSH_RETRY_INTERVAL", "10"))

# Telegram Configuration
ENABLE_NOTIFICATION = os.getenv("ENABLE_NOTIFICATION", "True")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Request configuration
POST_REQ_TIMEOUT = int(os.getenv("POST_REQ_TIMEOUT", "30"))

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
    """ANSI color codes for output"""

    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    BLUE = "\033[0;34m"
    YELLOW = "\033[0;33m"
    PURPLE = "\033[0;35m"
    CYAN = "\033[0;36m"
    NC = "\033[0m"


def convert_to_bool(value):
    """Converts a given value to a boolean"""
    value = value.lower()
    return value in ("y", "yes", "t", "true", "on", "1")


def check_requirements():
    """This function checks if all required environment variables are set"""
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


def update_stats(stat, host, pkg=None):
    """This function updates a single statistic"""
    data = stats[stat]
    if pkg is None:
        data.append(host)
    else:
        data.append((host, pkg))
    stats[stat] = data


def load_inventory(file_path):
    """Function for opening the inventory file."""
    with open(file_path, "r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def get_vm_status():
    """Function to get a list of the status of all VMs."""
    vms = proxmox.nodes(node).qemu.get()
    vm_status = {}
    for vm in vms:
        vm_status[vm["name"]] = {"id": vm["vmid"], "status": vm["status"]}
    return vm_status


def start_vm(vmid):
    """Function to start a VM."""
    print(f"Starting VM: {Style.BLUE}{vmid}{Style.NC}")
    proxmox.nodes(node).qemu(vmid).status().start.post(timeout=POST_REQ_TIMEOUT)


def stop_vm(vmid):
    """Function to stop a VM."""
    print(f"Stopping VM: {Style.BLUE}{vmid}{Style.NC}")
    proxmox.nodes(node).qemu(vmid).status().shutdown.post(timeout=POST_REQ_TIMEOUT)


def reboot_vm(vmid):
    """Function to reboot a VM."""
    proxmox.nodes(node).qemu(vmid).status().reboot.post(timeout=POST_REQ_TIMEOUT)


def ssh_command(host, command):
    """Function to execute a command via SSH."""
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
    """Function to check SSH availability."""
    start_time = time.time()
    total_attempts = SSH_TIMEOUT // SSH_RETRY_INTERVAL
    attempt = 0
    while time.time() - start_time < SSH_TIMEOUT:
        attempt = attempt + 1
        try:
            if total_attempts // 2 < attempt:
                style = Style.YELLOW
            else:
                style = Style.NC
            print(
                f"Attempting connection to {Style.BLUE}{host}{Style.NC}. "
                f"{style}{attempt}{Style.NC}/{total_attempts} attempts."
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
    """This function is for automatically detecting the distribution.
    This is done to set the correct update command
    """
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
            package_manager = f"sudo {lines[0]}"
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
            package_manager = f"sudo {lines[0]}"
            update_command = "upgrade -y"
            return distro, package_manager, update_command
    print(
        f"{Style.RED}Unsupported distro detected! "
        f"Could not set package manager!{Style.NC}"
    )
    return "unsupported", "unsupported", "unsupported"


def count_updated_packages(output, distro):
    """Function to count updated packages."""
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
        print(f"\n\{Style.GREEN}nPatchmanagement completed successfully{Style.NC}\n")
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
        command = f"{package_manager} update -y"
        exit_status, stdout, stderr = ssh_command(host, command)
        if exit_status != 0:
            print(
                f"{Style.RED}Error while updating the package database! "
                f"Skipping {Style.BLUE}{host}{Style.NC}"
            )
            update_stats("failed_patches", host)
            return False
    command = f"{package_manager} {update_command}"
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


def reboot_host(vm, distro):
    """Checks if a VM needs a reboot and restarts it"""
    host = vm["hostname"]
    vmid = vm["vmid"]
    reboot = vm["reboot"]
    print(f"Checking if reboot for {Style.BLUE}{host}{Style.NC} is necessary...")
    reboot_required = False
    if distro == "debian":
        command = "sudo ls -lah /var/run/reboot-required"
        exit_status, stdout, stderr = ssh_command(host, command)
        if exit_status == 0:
            reboot_required = True
    elif distro == "redhat":
        command = "sudo needs-restarting -r"
        exit_status, stdout, stderr = ssh_command(host, command)
        if exit_status == 1:
            reboot_required = True
    if reboot_required:
        if reboot:
            print(
                f"{Style.YELLOW}Reboot required on {Style.BLUE}{host}{Style.NC}. Rebooting now..."
            )
            reboot_vm(vmid)
        else:
            update_stats("needs_reboot", host)
    else:
        print(f"{Style.GREEN}No reboot required on {Style.BLUE}{host}{Style.NC}.")


def patch_vm(vm):
    """Patch a VM."""
    host = vm["hostname"]
    vmid = vm["vmid"]
    reboot = vm["reboot"]
    print(f"Waiting for SSH to become available on {host}...")
    if ssh_available(host):
        print(
            f"{Style.GREEN}SSH is available on {Style.BLUE}{host}{Style.NC}. "
            f"Looking for snapshot..."
        )
        if manage_snapshots(vmid, host):
            print(f"{Style.GREEN}Snapshot successfully created.{Style.NC}")
        else:
            print(
                f"{Style.RED}Skipping patch for {Style.BLUE}{host}{Style.RED}!{Style.NC}"
            )
            return False
        distro, package_manager, update_command = set_update_command(host)
        if distro == "unsupported":
            print(f"{Style.RED}Skipping patch for {Style.BLUE}{host}{Style.NC}")
            update_stats("unsupported", host)
            return False
        if patch_host(host, distro, package_manager, update_command):
            reboot_host(vm, distro)
            return True
        return False
    print(
        f"{Style.RED}SSH not available on {Style.BLUE}{host}{Style.RED} "
        f"after {SSH_TIMEOUT} seconds!{Style.NC} Skipping patch."
    )
    update_stats("ssh_failed_vms", host)
    return False


def main():
    """Main function to patch all VMs."""
    check_requirements()
    inventory = load_inventory(INVENTORY_FILE)
    virtual_machines = inventory["virtual_machines"]
    vm_status = get_vm_status()
    initially_stopped_vms = []
    for vm_name, patch_config in virtual_machines.items():
        if patch_config["patch"]:
            vmid = vm_status[vm_name]["id"]
            if vm_status[vm_name]["status"] == "stopped":
                start_vm(vmid)
                initially_stopped_vms.append(vmid)
    for vm_name, patch_config in virtual_machines.items():
        vm = {
            "vmid": vm_status[vm_name]["id"],
            "hostname": f"{vm_name}.{DOMAIN}",
            "reboot": patch_config["reboot"],
        }
        if patch_config["patch"]:
            if patch_vm(vm):
                print(
                    f"{Style.GREEN}Patching of {Style.BLUE}{vm["hostname"]} "
                    f"{Style.GREEN}complete{Style.NC}."
                )
            else:
                print(
                    f"{Style.RED}Patching of {Style.BLUE}{vm["hostname"]} "
                    f"{Style.RED}failed!{Style.NC}"
                )
        else:
            update_stats("manual_patches", vm["hostname"])
    for vmid in initially_stopped_vms:
        stop_vm(vmid)
    if convert_to_bool(ENABLE_NOTIFICATION):
        message = generate_notification(stats)
        send_telegram_message(message)


if __name__ == "__main__":
    main()
