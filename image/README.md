# Appliance image scripts

Tools to turn a prepared VM on an ESXi host into an `.ova`, describe it for the lab's template store and start a copy of it
with its configuration. The VM is prepared by `prepare-image.sh` and shut down before it is exported.

They are plain scripts (not a package) and need `pyvmomi` and `PyYAML`; `describe_template.py` needs only `PyYAML`.
Git preserves LF for the shell scripts, including when a source archive is created on Windows and unpacked on Debian.

## The ESXi password

Every script that talks to ESXi reads the password **only from the environment variable `ESXI_PASSWORD`**. There is no
argument for it (it would end up in the shell history and the process list), and it is never printed or logged.
For a manual run, read the selected host's password into the current shell and export it for the tools:

```bash
read -rs -p 'ESXi password: ' ESXI_PASSWORD
printf '\n'
export ESXI_PASSWORD
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
# Set ESXI_PASSWORD for this host as shown above.
python export_ova.py --host 10.20.20.202 --vm ginnungagap-image --out /data/ova --name Ginnungagap_Appliance
python describe_template.py /data/ova/Ginnungagap_Appliance.ova --cpu 2 --memory 4 --disk 20 --rename --write
# the .ova and the .yml now go into a folder of the template share; after the template API has listed it:
python set_guestinfo.py --host 10.20.20.202 --vm my-appliance --config ginnungagap.yml --ssh-key-file ~/.ssh/id_ed25519.pub --power-on
unset ESXI_PASSWORD
```

The tests (`tests/test_export_ova.py`) run without an ESXi: every step that does not need one is a plain function.

## Building the image, end to end

What was done (and verified on the lab ESXi) to get the OVA, in order:

1. A Debian 13 VM, installed with `sudo ./install.sh --repos-dir /srv/repos --install-packages --image --no-start`
   (Docker, compose, buildx, `python3-yaml`, `open-vm-tools`, `nfs-common`; the first-boot unit is enabled, SSH password
   login is switched on by a drop-in, there is no configuration).
   **An image built from a release** (the five tarballs of the release unpacked into `/srv/repos/<Repo>`, not `git archive`): put the
   release's `components.json` into `/srv/repos` too, after checking its signature and the checksums of the tarballs. `--adopt`
   (in step 2) then records the machine as that release, with the commits and checksums of the manifest, so `update --check` does
   not offer to replace code that already is the release's. On a base made from an older image first remove
   `/var/lib/ginnungagap/update` and `/var/lib/ginnungagap/components`, or the old state is kept (it did once: the image said 1.3.0).
2. `sudo image/prepare-image.sh --yes --zero-free-space` stops the setup watcher, removes the containers and volumes, then
   builds and pulls the images of every service for offline startup and removes every other tagged Docker image
   (`ginnungagap build-images --remove-other-images`: a service of an older bundle, such as the dropped `ova-deploy`, must not
   stay in the image). It removes the configuration, staged and rejected Wizard submissions, containers,
   volumes, SSH keys, the generated admin SSH password, the login account's password hash and shadow backups (root's password is locked too), the journal of the build boots, machine ID,
   DHCP leases, passwordless sudo rules, histories and logs, the image builder's own leftovers (`*.log`, `*.bak` and `old-repos-listing` in `/root`,
   the same known files in the login account's home; its other files stay) and the base VM's host name (reset to `ginnungagap`; guestinfo sets the real one at the first boot). The login account stays in the image so each clone can set up
   its own SSH access at first boot. Shut the VM down with
   `systemctl poweroff` and keep it off until export to avoid creating new keys and a machine ID inside the image.
   `--dry-run` lists the steps.
3. `export_ova.py` on a machine that can reach the ESXi host. It also removes the CPU/memory allocation settings
   (`cpuAllocation.*`, `memoryAllocation.*`) from the descriptor: a host that is managed by a vCenter refuses them on import
   ("access to resource settings on the host is restricted to the server that is managing it"). `--os-type debian12_64Guest`
   sets the guest type (an exported VM may carry the generic one). `--repack OLD.ova` applies the same corrections to an OVA
   exported earlier, without any host.
4. The `.ova` goes into a folder of the template share; `describe_template.py` writes the `<NAME>.yml` that makes the template
   API list it (leave that out and the OVA stays invisible in the GUI but can still be deployed by file name).

### The release and the update state in the image

The image knows which release it is, so a fresh appliance can run `ginnungagap update --check` right away. `prepare-image.sh` (steps
"record the installed release" and "remove the update access token, ...") does this:

* it keeps `installed.json` (with no "previous" release: a copy has nothing to roll back to), the unpacked releases that the links in
  `/var/lib/ginnungagap/components` point to, and those links; a machine that was installed with `install.sh --repos-dir` and not by
  `ginnungagap update --apply` gets `ginnungagap update --adopt --yes` first, which records the checkouts as the release it is;
* it **always deletes the update access token** (`/etc/ginnungagap/update-token` and the file `update.token_file` names; the token is
  optional and never belongs in an image), the update history, downloads, journal, audit log, pre-update backups (they contain
  the configuration with its API keys), the rollback image tags and every unpacked release that is not installed;
* `--dry-run` lists both steps.

Name the image after the appliance release it contains (`ginnungagap-appliance-v<version>`, from `ginnungagap/version.py`).

## Deploying a copy

Mjonyr's OVA import (the ESXi host pulls the OVA from the appliance's OVA server, see "OVA server for the ESXi hosts"
in the main README) imports it like any other template. Before the first power-on set the VM's guestinfo - the whole configuration, and, for access,
`--ssh-key-file` (and `--sudo-nopasswd` only for automation):

```bash
python set_guestinfo.py --host <esxi> --vm <new vm> --config ginnungagap.yml --ssh-key-file ~/.ssh/id_ed25519.pub --hostname <name> --power-on
```

At the first boot the VM creates its SSH host keys, applies hostname/key, creates the folder of a local template store, validates
the configuration and starts the services; the console login shows the address. Reached by IP or by any name, the gateway
answers with the web interface (a self-signed certificate; take the CA out as described in the main README).

Not verified here: importing into a host that is not managed by a vCenter, and a VM whose configuration uses a local template
store with real OVAs (the test used the lab's NFS export, 11 templates).

