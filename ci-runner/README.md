# homecontrol CI runners (Hyper-V, OpenTofu)

Three GitHub Actions self-hosted runners for `.github/workflows/test.yml` (`test` and `e2e` jobs,
`runs-on: [self-hosted, homecontrol-ci]`), each in its own Ubuntu 24.04 VM on Aaron's Windows 11 Pro desktop.
Cloned from Luna's runners (`aaronbuster19-coder/Loyalty-Rewards`, `ci-runner/` on `main`). The header of `main.tf`
is the full manual; this is the short version plus what we learned getting Luna's runners working.

The `deploy` job stays on dockerbox (`runs-on: [self-hosted, dockerbox]`): the VMs can't reach the LAN by design.
**Give dockerbox's runner the custom label `dockerbox`** (Settings → Actions → Runners → the runner → labels), or
deploys wait forever.

## What it builds, and what it only borrows

| | Owner | This stack |
|---|---|---|
| Switch `LunaCI` (Internal), NAT `LunaCI-NAT` on 192.168.250.0/24, PC = .1 | Luna's stack | **reads only**, stops if missing |
| Firewall rule `LunaCI-Block-VM-To-Host` (all inbound to the PC from the switch) | Luna's stack | **reads only**, stops if missing or disabled |
| VMs `homecontrol-ci`, `-2`, `-3` at **.30, .31, .32** (VM N = .(29+N), up to 10: .30–.39) | this stack | creates |
| Per-VM Hyper-V port ACLs + fail-closed guard | this stack | creates (identical to Luna's) |
| State `terraform.tfstate`, encrypted with its own `STATE_PASSPHRASE` | this stack | `C:\Hyper-V\HomecontrolCI` |

Windows allows **one NetNat per PC** and Luna owns it, so this stack never creates a switch, NAT or firewall rule.
`terraform_data.preflight` checks them on every apply (read-only `Get-*` cmdlets) before anything is downloaded or
built.

Addresses on the shared 192.168.250.0/24:

| Address | Used by |
|---|---|
| .1 | this PC (the gateway) |
| .10–.13 | Luna's runners `luna-ci` … `luna-ci-4` |
| .20 | the modelm runner VM (MAC 00-15-5D-4D-4D-01) |
| **.30–.39** | **this project: `RUNNER_COUNT` 1–10** (`local.ip_base = 29` in `main.tf`: moving the block is that one line) |

The 192.168.0.0/16 deny means these VMs can't reach Luna's runners or the .20 VM either, by design.

Each VM: Gen 2, Secure Boot (`MicrosoftUEFICertificateAuthority`), 2 vCPU, **1024 MB** static RAM (`MEMORY_MB`) plus
a 4 GB swap file, 80 GB disk, static IP, public DNS. Port ACLs deny 0.0.0.0/8, 10/8, 172.16/12 (the LAN, dockerbox
172.16.0.8), 192.168/16, 100.64/10 (tailnet), 169.254/16, 224/4, 240/4 and `::/0`, both directions, with one allow for
the gateway 192.168.250.1/32. The VM is created off; the guard starts it only after the ACLs are checked.

## Never

- **Never run `tofu destroy` in Luna's `ci-runner/`** for this project. Luna's destroy removes the shared `LunaCI`
  switch, `LunaCI-NAT` and firewall rule: this project's runners (and modelm's) go offline. Destroy Luna's stack
  only when every VM on the switch is gone or moving. Run every `tofu` command for this project in this folder.
- Never `tofu import` anything named `luna-ci*` / `LunaCI*` here, and never point `VM_DIR` at Luna's folder
  (apply refuses `...\LunaCI`). Two states, two passphrases.
- Never enable ephemeral mode or give the runner a PAT (`EPHEMERAL`, `ACCESS_TOKEN`). The one-hour registration
  token is the only credential.

## First run (PowerShell **"Run as administrator"**)

### Prerequisites (this project only)

Hyper-V, OpenTofu, qemu-img and WinRM on 127.0.0.1:5986 are already installed for Luna. On a PC that has none of them,
follow prerequisites 1–5 in the header of Luna's
[`ci-runner/main.tf`](https://github.com/aaronbuster19-coder/Loyalty-Rewards/blob/main/ci-runner/main.tf) first.

The one thing specific to this project is its own local admin for the provider, `homecontrol-tofu` (`HYPERV_USER`).
It also goes in **Hyper-V Administrators**: without it the provider fails with "Hyper-V was unable to find a virtual
machine" right after creating one (seen on the first apply here).

```powershell
$pw = Read-Host -AsSecureString 'Password for homecontrol-tofu'
New-LocalUser -Name homecontrol-tofu -Password $pw -PasswordNeverExpires -AccountNeverExpires
Add-LocalGroupMember -Group Administrators -Member homecontrol-tofu
Add-LocalGroupMember -Group 'Hyper-V Administrators' -Member homecontrol-tofu
```

### Apply

Check Luna's network first (apply also checks, and stops with "apply Luna's stack first" if any is missing):

```powershell
Get-VMSwitch LunaCI; Get-NetNat LunaCI-NAT; Get-NetFirewallRule -Name LunaCI-Block-VM-To-Host
git clone https://github.com/aaronbuster19-coder/homecontrol C:\HomecontrolCI\src   # outside Google Drive / OneDrive
cd C:\HomecontrolCI\src\ci-runner
copy .env.example .env; notepad .env       # homecontrol-tofu's password, a NEW state passphrase, ...
New-Item -ItemType Directory -Force C:\Hyper-V\HomecontrolCI\cidata
tofu init
# Fetch RUNNER_TOKEN now (below), paste it into .env, then straight away:
tofu apply -parallelism=1
```

Afterwards check Settings → Actions → Runners: only `homecontrol-ci-hyperv`, `-2`, `-3` should be new.

## Lessons from Luna (read before debugging)

1. **Always `tofu apply -parallelism=1`.** Building several seed ISOs at once fails with IMAPI COM error
   `0xC0AAB138`.
2. **PowerShell "Run as administrator".** Otherwise the guard refuses and leaves the VMs off.
3. **`RUNNER_TOKEN` lasts one hour** and is used only at first boot. Get it from this repo's Settings → Actions →
   Runners → New self-hosted runner (the value after `--token`), and apply straight away. A new token changes the
   seed, so the next apply rebuilds the VMs. **New token + `tofu apply -parallelism=1` is the reset button.**
4. **DNS is the usual cause of offline runners.** If the VPN app (Mullvad) is running, even disconnected, it can
   block outgoing DNS on the host; the VMs then resolve nothing and jobs fail at checkout with
   `Could not resolve host: github.com`. On the host:
   ```powershell
   Resolve-DnsName github.com -Server 1.1.1.1 -DnsOnly
   Get-NetNatSession | ? InternalSourceAddress -like '192.168.250.3*' | group InternalSourceAddress, ExternalDestinationPort
   ```
   Healthy VMs show ports 443 and 80, not only 53.
5. **Keep the runner image current.** GitHub stops sending jobs to a runner more than 30 days behind the latest
   release, and auto-update is deliberately off. At least monthly: bump the tag and digest in `docker-compose.yml`
   (Docker Hub `myoung34/github-runner`, tag `<version>-ubuntu-noble`, pinned by the index digest), then do a
   token + apply reset.
6. **No login, by design**: no password, no SSH key, Guest Services and KVP off, so
   `Get-VM homecontrol-ci* | Get-VMNetworkAdapter` shows no IP addresses. Diagnose from the host:
   `Get-VM homecontrol-ci*` (Heartbeat), `Get-NetNeighbor -InterfaceAlias 'vEthernet (LunaCI)'` (is .30… seen on the
   switch?), the NAT sessions above, `vmconnect localhost homecontrol-ci` (console: boot and cloud-init messages),
   and the GitHub Runners page.
7. **Never ephemeral mode or a PAT** in the runner (above).

## Docker settings inside the VMs (unchanged from Luna; each came from a real failure)

- `/etc/docker/daemon.json`: `bip 10.200.0.1/24`, address pools in 10.201.0.0/16, `dns: ["10.200.0.1"]`.
- systemd-resolved `DNSStubListenerExtra=10.200.0.1`, so containers can use the VM as resolver.
- Hyper-V router guard **off** (on, it drops Docker's NAT'd traffic); the port ACLs are the wall.
- Bridged container traffic gets no DNS behind the host NAT, so the workflow uses `docker build --network=host` and
  runs the e2e container with `--network=host` (`E2E_DOCKER_ARGS`).

## Capacity (recommendation, not applied)

The `e2e` job's container alone uses about 500–650 MB while the suite runs (Chromium, the app, the fake HA and
pytest; measured with `docker stats` on the v10/ops branch). On top of that are the OS, dockerd and the runner
container. With `MEMORY_MB=1024`, a VM running e2e is at or past its RAM and pages to the 4 GB swap file. That
makes the browser tests slow and bursty, which is when timing-sensitive tests failed (brightness throttle, import
dialog, theme socket timeout).

The tests now wait on real conditions with generous ceilings, so they pass under that pressure: the `e2e-slow` job
runs the whole suite with `--cpus=1` on every push. The VMs are still the bottleneck for speed. If the PC has the
memory to spare next to Luna's VMs, the recommended change is:

- **`MEMORY_MB=2048`** in `.env` (then token + `tofu apply -parallelism=1`). This is the change that matters: e2e
  stays out of swap. Three VMs then hold 6 GB.
- **Keep `RUNNER_COUNT=3`**. A push now runs `test`, `e2e` and `e2e-slow` at the same time, one per VM. A 4th runner
  would only help when two pushes overlap, which `cancel-in-progress` mostly avoids anyway.
- **Keep 2 vCPU.** CPU wasn't the limit (`--cpus=1` passes), and Hyper-V vCPUs are shared with the desktop anyway.

## Names

| Luna | here |
|---|---|
| VMs `luna-ci`, `luna-ci-N` (.10–.13) | `homecontrol-ci`, `homecontrol-ci-N` (.30–.39) |
| MACs `00:15:5D:4C:43:xx` ("LC") | `00:15:5D:48:43:xx` ("HC"; .20 is `4D:4D:01`) |
| `VM_DIR` `C:\Hyper-V\LunaCI` | `C:\Hyper-V\HomecontrolCI` (disks `disk\homecontrol-ci*-os.vhdx`, seeds `cidata\`) |
| runner `luna-ci-hyperv`, label `luna-ci` | `homecontrol-ci-hyperv`, label `homecontrol-ci` |
| `/opt/luna-ci-runner`, `luna-ci-compose.service` | `/opt/homecontrol-ci-runner`, `homecontrol-ci-compose.service` |
| `/etc/cron.daily/luna-ci-prune`, `resolved.conf.d/luna-ci-docker.conf` | `homecontrol-ci-prune`, `homecontrol-ci-docker.conf` |
| tofu user `luna-tofu` | `homecontrol-tofu` |
| guard/provisioner env vars `LUNA_*` | `HC_*` |

## Retiring

`tofu destroy` here (leaves Luna's network alone), delete `C:\Hyper-V\HomecontrolCI` and the clone, remove the
runners in GitHub, `Remove-LocalUser homecontrol-tofu`. Leave WinRM while Luna uses it.
