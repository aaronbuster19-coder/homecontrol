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
| VMs `homecontrol-ci`, `-2`, `-3` at **.30, .31, .32** (`IP_FIRST`, `RUNNER_COUNT`) | this stack | creates |
| Per-VM Hyper-V port ACLs + fail-closed guard | this stack | creates (identical to Luna's) |
| State `terraform.tfstate`, encrypted with its own `STATE_PASSPHRASE` | this stack | `C:\Hyper-V\HomecontrolCI` |

Windows allows **one NetNat per PC** and Luna owns it, so this stack never creates a switch, NAT or firewall rule.
`terraform_data.preflight` checks them on every apply (read-only `Get-*` cmdlets) before anything is downloaded or
built.

Addresses on the switch: .1 this PC, .10–.13 Luna, .20 the modelm runner, .30+ this project. `local.taken` in
`main.tf` lists the used ones and apply refuses an overlap; add any new VM there.

Each VM: Gen 2, Secure Boot (`MicrosoftUEFICertificateAuthority`), 2 vCPU, **1024 MB** static RAM (`MEMORY_MB`) plus
a 4 GB swap file, 80 GB disk, static IP, public DNS. Port ACLs deny 0.0.0.0/8, 10/8, 172.16/12 (the LAN, dockerbox
172.16.0.8), 192.168/16, 100.64/10 (tailnet), 169.254/16, 224/4, 240/4 and `::/0`, both directions, with one allow for
the gateway 192.168.250.1/32. The VM is created off; the guard starts it only after the ACLs are checked.

## Never

- **Never run `tofu destroy` in Luna's `ci-runner/`** for this project. Luna's destroy removes the shared `LunaCI`
  switch, `LunaCI-NAT` and firewall rule: these VMs (and modelm's) lose the internet. Destroy Luna's stack only
  when every VM on the switch is gone or moving.
- Never `tofu import` anything named `luna-ci*` / `LunaCI*` here, and never point `VM_DIR` at Luna's folder
  (apply refuses `...\LunaCI`). Two states, two passphrases.
- Never enable ephemeral mode or give the runner a PAT (`EPHEMERAL`, `ACCESS_TOKEN`). The one-hour registration
  token is the only credential.

## First run (PowerShell **"Run as administrator"**)

Prerequisites are Luna's and already done: Hyper-V, OpenTofu, qemu-img, WinRM on 127.0.0.1:5986, and the local
admin `luna-tofu` (reused here). Check Luna's network first:

```powershell
Get-VMSwitch LunaCI; Get-NetNat LunaCI-NAT; Get-NetFirewallRule -Name LunaCI-Block-VM-To-Host
git clone https://github.com/aaronbuster19-coder/homecontrol C:\HomecontrolCI\src   # outside Google Drive / OneDrive
cd C:\HomecontrolCI\src\ci-runner
copy .env.example .env; notepad .env       # luna-tofu's password, a NEW state passphrase, ...
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
   Get-NetNatSession | ? InternalSourceAddress -like '192.168.250.*' | group InternalSourceAddress, ExternalDestinationPort
   ```
   Healthy VMs show port 443, not only 53. (This project's VMs are the .30+ addresses.)
5. **Keep the runner image current.** GitHub stops sending jobs to a runner more than 30 days behind the latest
   release, and auto-update is deliberately off. At least monthly: bump the tag and digest in `docker-compose.yml`
   (Docker Hub `myoung34/github-runner`, tag `<version>-ubuntu-noble`, pinned by the index digest), then do a
   token + apply reset.
6. **No login, by design**: no password, no SSH key, Guest Services and KVP off. Diagnose from the host:
   `Get-VM homecontrol-ci*` (Heartbeat), the NAT sessions above, `vmconnect localhost homecontrol-ci` (console:
   boot and cloud-init messages), and the GitHub Runners page.
7. **Never ephemeral mode or a PAT** in the runner (above).

## Docker settings inside the VMs (unchanged from Luna; each came from a real failure)

- `/etc/docker/daemon.json`: `bip 10.200.0.1/24`, address pools in 10.201.0.0/16, `dns: ["10.200.0.1"]`.
- systemd-resolved `DNSStubListenerExtra=10.200.0.1`, so containers can use the VM as resolver.
- Hyper-V router guard **off** (on, it drops Docker's NAT'd traffic); the port ACLs are the wall.
- Bridged container traffic gets no DNS behind the host NAT, so the workflow uses `docker build --network=host` and
  runs the e2e container with `--network=host` (`E2E_DOCKER_ARGS`).

## Names

| Luna | here |
|---|---|
| VMs `luna-ci`, `luna-ci-N` (.10–.13) | `homecontrol-ci`, `homecontrol-ci-N` (`IP_FIRST`…) |
| MACs `00:15:5D:4C:43:xx` | `00:15:5D:48:43:xx` |
| `VM_DIR` `C:\Hyper-V\LunaCI` | `C:\Hyper-V\HomecontrolCI` (disks `disk\homecontrol-ci*-os.vhdx`, seeds `cidata\`) |
| runner `luna-ci-hyperv`, label `luna-ci` | `homecontrol-ci-hyperv`, label `homecontrol-ci` |
| `/opt/luna-ci-runner`, `luna-ci-compose.service` | `/opt/homecontrol-ci-runner`, `homecontrol-ci-compose.service` |
| `/etc/cron.daily/luna-ci-prune`, `resolved.conf.d/luna-ci-docker.conf` | `homecontrol-ci-prune`, `homecontrol-ci-docker.conf` |

## Retiring

`tofu destroy` here (leaves Luna's network alone), delete `C:\Hyper-V\HomecontrolCI` and the clone, remove the
runners in GitHub. Leave `luna-tofu` and WinRM while Luna uses them.
