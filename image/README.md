# Appliance image scripts

Tools to turn a prepared VM on an ESXi host into an `.ova`, describe it for the lab's template store and start a copy of it
with its configuration. The VM is prepared by `prepare-image.sh` and shut down before it is exported.

They are plain scripts (not a package) and need `pyvmomi` and `PyYAML`; `describe_template.py` needs only `PyYAML`.

## The ESXi password

Every script that talks to ESXi reads the password **only from the environment variable `ESXI_PASSWORD`**. There is no
argument for it (it would end up in the shell history and the process list), and it is never printed or logged. Load it where
it already lives, for example from the live app's `.env` on the testbox:

```bash
set -a; . ~/TopologyOperator-livetest/.env; set +a
```

## Scripts

* `export_ova.py` - exports a **powered-off** VM: takes an export lease, downloads every disk as a streamOptimized VMDK
  (keeping the lease alive while it runs), asks the host for the OVF descriptor, renames the network inside it to
  `--network-name` (default `Network`, so the importer can map it to any port group), writes the SHA-256 manifest and packs
  a ustar `.ova` with the descriptor first. Checks free space first, aborts the lease and removes its temporary files on any
  error, prints the size and SHA-256 of the result.
* `describe_template.py` - writes the YAML description for the template store (`id` = SHA-256 in capitals, file name
  `<NAME>_<SHA256>.ova`, size, cpu, memory, disk, users, hostname). `--rename` renames the `.ova` the way the store wants it,
  `--write` writes `<NAME>.yml` next to it. It writes **no password**: `note` is free text that says the login has to be
  changed.
* `set_guestinfo.py` - sets `guestinfo.ginnungagap.config` (the configuration file, base64, with
  `guestinfo.ginnungagap.config.encoding = base64`) and optionally `guestinfo.ginnungagap.ssh_authorized_key` and
  `guestinfo.ginnungagap.hostname` on a VM, and with `--power-on` starts it. Only the key names are printed, never a value
  (the configuration may hold API keys).

## Example

```bash
set -a; . ~/TopologyOperator-livetest/.env; set +a        # ESXI_PASSWORD
python export_ova.py --host 10.20.20.202 --vm ginnungagap-image --out /data/ova --name Ginnungagap_Appliance
python describe_template.py /data/ova/Ginnungagap_Appliance.ova --cpu 2 --memory 4 --disk 20 --rename --write
# the .ova and the .yml now go into a folder of the template share; after the template API has listed it:
python set_guestinfo.py --host 10.20.20.202 --vm my-appliance --config ginnungagap.yml --ssh-key-file ~/.ssh/id_ed25519.pub --power-on
```

The tests (`tests/test_export_ova.py`) run without an ESXi: every step that does not need one is a plain function.

## Building the image, end to end

What was done (and verified on the lab ESXi) to get the OVA, in order:

1. A Debian 13 VM, installed with `sudo ./install.sh --repos-dir /srv/repos --install-packages --image --no-start`
   (Docker, compose, buildx, `python3-yaml`, `open-vm-tools`, `nfs-common`; the first-boot unit is enabled, there is no
   configuration).
2. `sudo image/prepare-image.sh --yes --zero-free-space` stops the setup watcher, then builds and pulls the images of
   every service for offline startup. It removes the configuration, staged and rejected Wizard submissions, containers,
   volumes, SSH keys, machine ID, DHCP leases, passwordless sudo rules, histories and logs. Shut the VM down with
   `systemctl poweroff` and keep it off until export to avoid creating new keys and a machine ID inside the image.
   `--dry-run` lists the steps.
3. `export_ova.py` on a machine that can reach the ESXi host. It also removes the CPU/memory allocation settings
   (`cpuAllocation.*`, `memoryAllocation.*`) from the descriptor: a host that is managed by a vCenter refuses them on import
   ("access to resource settings on the host is restricted to the server that is managing it"). `--os-type debian12_64Guest`
   sets the guest type (an exported VM may carry the generic one). `--repack OLD.ova` applies the same corrections to an OVA
   exported earlier, without any host.
4. The `.ova` goes into a folder of the template share; `describe_template.py` writes the `<NAME>.yml` that makes the template
   API list it (leave that out and the OVA stays invisible in the GUI but can still be deployed by file name).

## Deploying a copy

The OVA deploy service (`POST /deploy/ova`, `ova_filename`, `datastore`, `network` = `{"Network": "<port group>"}`) imports it
like any other template. Before the first power-on set the VM's guestinfo - the whole configuration, and, for access,
`--ssh-key-file` (and `--sudo-nopasswd` only for automation):

```bash
python set_guestinfo.py --host <esxi> --vm <new vm> --config ginnungagap.yml --ssh-key-file ~/.ssh/id_ed25519.pub --hostname <name> --power-on
```

At the first boot the VM creates its SSH host keys, applies hostname/key, creates the folder of a local template store, validates
the configuration and starts the services; the console login shows the address. Reached by IP or by any name, the gateway
answers with the web interface (a self-signed certificate; take the CA out as described in the main README).

Not verified here: importing into a host that is not managed by a vCenter, and a VM whose configuration uses a local template
store with real OVAs (the test used the lab's NFS export, 11 templates).

