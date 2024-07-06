# Proxmox-VM-Patchmanagement

[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black) [![linting: pylint](https://img.shields.io/badge/linting-pylint-yellowgreen)](https://github.com/pylint-dev/pylint)

This script manages automatic patching of VMs hosted on Proxmox. It supports Red Hat and Debian based distros, which are automatically detected and handled accordingly.

If wanted, the script sends a Telegram notification after the patching process has been finished. It also prints out the whole process, as well as a summary at the end.

The patching process looks like this:
1. Collect the status (started or stopped) of all VMs, which have patching enabled
1. Start all stopped VMs
1. Try to connect to the VM via SSH
    1. If the SSH connection is successful, delete the latest snapshot of the VM and create a new one
        * If the snapshot creation fails, skip patching
    1. Detect the distribution to set the correct package manager and update commands
    1. Patch the VM
    1. Check if a reboot is necessary
        * If a reboot is necessary and rebooting is enabled, reboot the VM
1. Shutdown all previously stopped VMs
1. Send a notification with a patch summary


The script is meant to be used in a CI/CD pipeline, but manual usage is also possible.

I don't know much about Python, so I set myself a challenge to automate my homelab further and learn something in the process.
It might not be the prettiest, or most efficient code, but I did my best to make it readable.

## Requirements

You'll need to create an API user for Proxmox and give it the required permissions. To do this, you'll have to first create the user under `Datacenter > Permissions > Users`.
After creating the user, you'll have to create a new role. This can be done under `Datacenter > Permissions > Users`. The required permissions for this role are:
```
VM.Audit
VM.Monitor
VM.PowerMgmt
VM.Snapshot
```

After this is done, create a new group under `Datacenter > Permissions > Groups` and add the user you created in the first step.

Last, but not least, you'll have to map the permissions to the created group. To do that, navigate to `Datacenter > Permissions`, click `Add > Group Permission`, select `/` as path, add your goup and role and you're done.

To install the dependecies for the script, you'll have to run `pip install -r requirements.txt`.

## Usage

To use the script, you'll first have to create an inventory file. The file structure has to be as follows:

```yaml
virtual_machines:
  host:
    patch: <true|false>
    reboot: <true|false>
```

An example can be found [here](inventory.yml.example).

After creating the inventory file, multiple environment variables have to be set:

| Variable            | Description                                                                           | Required                               | Default       |
|---------------------|---------------------------------------------------------------------------------------|----------------------------------------|---------------|
| INVENTORY_FILE      | Path to your inventory file                                                           | No                                     | inventory.yml |
| PROXMOX_HOST        | FQDN of your Proxmox host                                                             | Yes                                    |               |
| PROXMOX_USER        | The user which is used to connect to the Proxmox API                                  | Yes                                    |               |
| PROXMOX_PASSWORD    | The password for the user                                                             | Yes                                    |               |
| PROXMOX_VERIFY_SSL  | Trigger to enable/disable SSL verification. Set to false for self signed certificates | No                                     | false         |
| SSH_USER            | The user used to connect to the VMs                                                   | Yes                                    |               |
| SSH_KEY_FILE        | The path to the SSH key file used for authentication                                  | Yes                                    |               |
| SSH_TIMEOUT         | Timeout of SSH login                                                                  | No                                     | 300           |
| SSH_RETRY_INTERVAL  | Retry interval for the SSH availability check                                         | No                                     | 10            |
| ENABLE_NOTIFICATION | Enables notification message via Telegram                                             | No                                     | true          |
| TELEGRAM_BOT_TOKEN  | The authentication token of your Telegram bot                                         | Only if `ENABLE_NOTIFICATION` is true  |               |
| TELEGRAM_CHAT_ID    | The channel identifier to send the message to                                         | Only if `ENABLE_NOTIFICATION` is true  |               |
| POST_REQ_TIMEOUT    | Timeout for post requests                                                             | No                                     | 30            |
| ENABLE_PATCH_OUTPUT | Prints stdout of update command in pipeline                                           | No                                     | false         |
| DOMAIN              | Your domain. Is used to create the FQDN of the hosts to patch                         | Yes                                    |               |

To run the script, simply execute `python3 patchmanagement.py`.

If you use a self signed (and therefore untrusted) certificate for your Proxmox instance and the SSL verification warnings annoy you, you can simply add `-W ignore` to the command to supress these warnings.

## Restrictions

The script assumes you have a working DNS setup. The script won't work with plain IPs as hosts in the inventory and support for that is not planned. However I'll gladly accept pull requests if you want to implement that.

## Known issues

Sometimes snapshot creation fails due to Proxmox closing the connection without an answer.
I wasn't able to find out why that happens yet, but the script is smart enough to not patch a VM if the snapshot creation failed, so it won't break a VM by patching it without having a way to roll back the changes.
