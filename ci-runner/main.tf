# homecontrol CI runner VMs: free GitHub Actions self-hosted runners for .github/workflows/test.yml, in Hyper-V VMs
# on Aaron's Windows 11 Pro desktop. OpenTofu with the taliesins/hyperv provider.
#
# A CLONE OF LUNA'S RUNNER (aaronbuster19-coder/Loyalty-Rewards, ci-runner/main.tf on main), adapted to SHARE Luna's
# network. Windows allows ONE NetNat per PC and Luna's stack owns it, so this stack creates no switch, no NAT and no
# firewall rule: it only reads them, and stops (before touching anything) if they are missing. Everything this stack
# does create carries its own names (homecontrol-ci*), its own addresses and its own state. ci-runner/README.md is the
# short version of this header, with the lessons from getting Luna's runners working.
#
# WHAT `tofu apply` MAKES (all files under VM_DIR from .env, nothing in this repo):
#   - Nothing on the network. It USES, read-only, what Luna's stack made:
#       the Internal switch "LunaCI", the host NAT "LunaCI-NAT" (192.168.250.0/24; .1 is this PC) and the Windows
#       Firewall rule "LunaCI-Block-VM-To-Host" (all inbound to this PC on vEthernet (LunaCI) blocked, whatever the
#       source address claims to be). terraform_data.preflight checks all three, and the gateway address, before
#       anything is built, and again before every apply. WinNAT has no DHCP, so the VMs get static addresses and
#       public DNS (1.1.1.1, 9.9.9.9).
#   - The Ubuntu 24.04 generic cloud image (release 20260926), SHA-256 pinned and checked. qemu-img turns it into a
#     plain VHD and Hyper-V's own Convert-VHD writes the VHDX (dynamic, 1 MB blocks), which is grown to 80 GB.
#   - A NoCloud seed ISO (CIDATA) whose cloud-init installs Docker CE (signing key pinned below), writes
#     docker-compose.yml and a runner-only .env into /opt/homecontrol-ci-runner and starts the runner (label
#     homecontrol-ci) through a systemd unit that retries every minute until `docker compose up` succeeds, on every boot.
#   - One VM per runner (RUNNER_COUNT in .env, 1 to 4): "homecontrol-ci" (192.168.250.30), then "homecontrol-ci-2"
#     (.31), "homecontrol-ci-3" (.32)... (IP_FIRST in .env moves the block). Each has its own disk, seed ISO and guard.
#     Addresses already in use on the switch (Luna's .10-.13, the modelm runner's .20: local.taken below) are refused.
#     The deny list stops the VMs reaching each other, Luna's VMs and the modelm VM too.
#   - Each VM ("homecontrol-ci" below): Generation 2, Secure Boot (MicrosoftUEFICertificateAuthority), 2 vCPU, 4 GB
#     static RAM, no checkpoints, starts with Windows. Guest Service Interface, Key-Value Pair Exchange and VSS are off;
#     no shared folders. Static MAC (00:15:5D:48:43:xx, "HC"; Luna's are 00:15:5D:4C:43:xx, so the two stacks never
#     share one), MAC spoofing off, DHCP guard on. Router guard OFF: it treats Docker's NAT'd container traffic as
#     routing and drops it (containers and docker build had no DNS on Luna's VMs); the port ACLs keep the VM in.
#   - Hyper-V port ACLs on the VM's network adapter, EXACTLY as Luna's: deny 0.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12
#     (the LAN, dockerbox 172.16.0.8), 192.168.0.0/16 (includes this PC's 192.168.250.1 and every other VM on the
#     switch), 100.64.0.0/10 (tailnet), 169.254.0.0/16, 224.0.0.0/4 (multicast), 240.0.0.0/4 (includes broadcast
#     255.255.255.255) and all IPv6, both directions, plus ONE allow rule for 192.168.250.1/32, the gateway: without it
#     the 192.168.0.0/16 deny also cuts the VM off from the gateway every packet to the internet goes through (seen on
#     Luna's first run, 6 Oct 2026: no ARP, no internet, the runner never registered). Luna's firewall rule still
#     blocks the VM from connecting to this PC itself. Only the internet is left. These sit on the Hyper-V switch port,
#     outside the VM, so root inside the VM can't remove them.
#   - Fail-closed start: the VM is created switched off with "start with Windows" off. terraform_data.guard (at the
#     end) switches it off if it is running, adds any missing deny rule (and the gateway allow) BEFORE removing any
#     stray rule (so the list is never down), checks everything, and only then turns "start with Windows" on and
#     starts it. If any step fails the VM stays off and will not start at the next Windows boot.
#
# PREREQUISITES (once, in PowerShell "Run as administrator"). Luna's runners are already up on this PC, so 1-5 are
# done; only check them:
#   1. Windows 11 Pro with Hyper-V.
#   2. OpenTofu:  winget install --exact --id OpenTofu.Tofu
#   3. This project's OWN local admin for the provider, "homecontrol-tofu" (HYPERV_USER/HYPERV_PASSWORD in .env), so
#      it never shares Luna's luna-tofu password or profile. Also in "Hyper-V Administrators": the provider runs most
#      steps in a plain (non-elevated) WinRM shell, where an admin without that group can see no VMs ("Hyper-V was
#      unable to find a virtual machine" right after creating it). WinRM (step 5) needs nothing per account.
#        $pw = Read-Host -AsSecureString 'Password for homecontrol-tofu'
#        New-LocalUser -Name homecontrol-tofu -Password $pw -PasswordNeverExpires -AccountNeverExpires
#        Add-LocalGroupMember -Group Administrators -Member homecontrol-tofu
#        Add-LocalGroupMember -Group 'Hyper-V Administrators' -Member homecontrol-tofu
#   4. qemu-img:  winget install --exact --id SoftwareFreedomConservancy.QEMU   (QEMU_IMG in .env points at it).
#   5. WinRM over HTTPS on 127.0.0.1:5986 with NTLM, set up for Luna (Loyalty-Rewards ci-runner/main.tf, step 5).
#      That set LocalAccountTokenFilterPolicy=1 (UAC's remote restrictions off for every local admin); it stays until
#      the LAST of the two stacks is retired.
#   6. Luna's stack applied and healthy: the switch, NAT and firewall rule must exist. Check:
#        Get-VMSwitch LunaCI; Get-NetNat LunaCI-NAT; Get-NetFirewallRule -Name LunaCI-Block-VM-To-Host
#      Apply stops with a clear message if any is missing (terraform_data.preflight). It never creates them.
#   7. Free addresses: no other VM on the LunaCI switch may use IP_FIRST .. IP_FIRST+RUNNER_COUNT-1. local.taken
#      lists the known ones; add any new VM there.
#
# USE (PowerShell "Run as administrator": the guard's Hyper-V cmdlets and port ACLs need admin; from a normal
# PowerShell the guard refuses and leaves the VMs off).
# Run it from a clone OUTSIDE Google Drive / OneDrive, never from a Drive-synced folder: .env holds the WinRM admin
# password and other secrets, and main.tf and the provider binaries in .terraform\ run as administrator, so anything
# synced into that folder would become code running as admin on this PC. For example:
#   git clone https://github.com/aaronbuster19-coder/homecontrol C:\HomecontrolCI\src
#   cd C:\HomecontrolCI\src\ci-runner
#   copy .env.example .env          then edit .env (notepad .env). Never commit .env; it lives only in this clone.
#   New-Item -ItemType Directory -Force C:\Hyper-V\HomecontrolCI\cidata
#                                   VM_DIR (from .env) and its cidata folder must exist before plan/apply: the seed
#                                   zip is written there while planning, before any apply step can make folders.
#   tofu init                       downloads the providers into ci-runner\.terraform (see "Known traps")
#   (fetch a fresh RUNNER_TOKEN into .env: it lasts one hour)
#   tofu apply -parallelism=1       ALWAYS -parallelism=1: building several seed ISOs at once fails with IMAPI COM
#                                   error 0xC0AAB138 (seen on Luna). About 10-20 minutes the first time; the runners
#                                   show as Idle in GitHub > Settings > Actions > Runners when first boot is done.
#                                   Check that list then: only RUNNER_NAME, RUNNER_NAME-2 ... up to RUNNER_COUNT
#                                   should be there (see "HONEST LIMITS").
#   tofu destroy                    removes THIS stack's VMs, disks, ISOs and image. Never the switch, NAT or
#                                   firewall rule: this stack doesn't own them. VM_DIR keeps the encrypted state and
#                                   cidata\cidata.zip (which holds the runner token): delete it when done for good.
# Run every tofu command from THIS folder. Never run `tofu destroy` in Luna's ci-runner folder on this project's
# behalf: Luna's destroy removes the LunaCI switch, NAT and firewall rule, which cuts these VMs off too (and the
# modelm runner). Destroy Luna's stack only when every VM on the switch is gone or moving elsewhere.
#
# CHANGES AND RESETS: anything that changes the seed (docker-compose.yml, any runner key in .env, cloud-init below)
# makes the next `tofu apply` destroy the VM and its disk and build fresh ones. A fresh VM must register again, so put
# a fresh RUNNER_TOKEN in .env first. That is also the reset button: new token + `tofu apply -parallelism=1` = clean
# machine. Do it at least monthly anyway, when bumping the runner image (docker-compose.yml) within GitHub's 30-day
# window. Other VM settings in this file (memory, notes, ...) are changed in place: the provider switches the VM off
# and back on, and the guard then switches it off again, re-checks the wall and starts it.
#
# RETIRING: `tofu destroy` (here), delete VM_DIR and the clone, remove the runners in GitHub,
# Remove-LocalUser homecontrol-tofu. Leave WinRM and Luna's network alone while Luna still uses them (Luna's main.tf RETIRING undoes those).
#
# HONEST LIMITS:
#   - Anyone who can push a branch or open a same-repo pull request (and every Dependabot update) runs code as root
#     in these VMs, with the VM's Docker. The VM is the wall: it can reach the internet only.
#   - The VM is persistent. A job can leave things behind (in Docker, /tmp, the runner container) that later jobs see.
#     There is no nightly reset, and deliberately no EPHEMERAL mode: it would need a long-lived GitHub token with
#     Administration: write inside the VM (where every job can read it) and still would not reset the VM. Only a
#     rebuild (above) gives a clean machine.
#   - A Hyper-V escape would reach this PC. Keep Windows Update on: it patches Hyper-V.
#   - These VMs depend on Luna's network. If Luna's stack is destroyed or rebuilt, they lose the internet until it is
#     back (their port ACLs stay; nothing opens up). A new LunaCI switch has a new identity: re-apply here afterwards
#     so the VMs' adapters are reconnected and the guard re-checks.
#   - The LunaCI firewall rule and NAT are checked at apply time only. If someone deletes the firewall rule later, the
#     port ACL deny on 192.168.0.0/16 still blocks the VMs from this PC; the rule is the second wall.
#   - 2 vCPU / 4 GB is smaller than GitHub's hosted runners. Memory is held all the time, on top of Luna's VMs: check
#     free memory before raising RUNNER_COUNT. When the PC is off, jobs wait (and fail after 24 hours queued).
#   - Jobs can read the runner's credentials: its saved registration, and RUNNER_TOKEN (container environment, the
#     VM's .env and the seed ISO, which stays attached). The registration token stays valid for its full hour and can
#     register MORE runners in that time, so for the first hour a job could add a rogue runner. That is why it is
#     fetched right before apply and why you check the runner list afterwards. After the hour it is worthless.
#   - Tested here only as text: OpenTofu fmt and validate. Nothing of this copy has run on the real host yet; the first
#     `tofu apply` and the first CI run are the real test.
#
# KNOWN TRAPS (taliesins/hyperv 1.2.1):
#   - After a failed step the provider can leave HYPERV_PASSWORD in a temporary file
#     C:\Users\<HYPERV_USER>\AppData\Local\Temp\elevated-shell-terraform-*.ps1. Delete those after any failed apply.
#   - A half-failed apply can leave a VM, disk or ISO that exists in Hyper-V but not in the state. The next apply then
#     stops with "already exists - ... import". Delete the leftover by hand (Hyper-V Manager, or remove the file under
#     VM_DIR) or `tofu import` it, then apply again. Never import anything named luna-ci* or LunaCI*: Luna's state owns
#     those, and two states managing one object will fight (and this stack's destroy would delete Luna's).
#   - Copying the 2 GB base disk can be slow, so the disk's create timeout is raised from the default 5 minutes.
#     On Windows 11 a Get-VHD right after the copy can fail with "object is in use" (provider issue #188): apply again.
#   - `tofu init` creates ci-runner\.terraform\ (provider binaries) and ci-runner\.terraform.lock.hcl. .gitignore keeps
#     .terraform\ out of git (to keep it out of the clone entirely, set $env:TF_DATA_DIR = 'C:\Hyper-V\HomecontrolCI\.terraform'
#     before init). The lock file is normally committed; your call.
#   - The state (which holds the runner token) lives in VM_DIR\terraform.tfstate, encrypted with STATE_PASSPHRASE (its
#     OWN passphrase, not Luna's). .gitignore ignores *.tfstate*. Don't use `tofu plan -out` (saved plans are not
#     encrypted here), and if tofu ever writes an errored.tfstate into ci-runner\ after a failed state save, move it
#     out and delete it.
#   - Paths use backslashes and consistent case everywhere; forward slashes make the provider replace the VM.
#   - Integration-service names are the English ones. On a non-English Windows, check Get-VMIntegrationService.

terraform {
  required_version = ">= 1.8.0"

  required_providers {
    hyperv = {
      source  = "taliesins/hyperv"
      version = "1.2.1" # newest release (2024-02-11); https://github.com/taliesins/terraform-provider-hyperv
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.8"
    }
  }

  # State outside the repo, next to the VM. OpenTofu evaluates this early from the .env map below.
  backend "local" {
    path = "${local.env["VM_DIR"]}\\terraform.tfstate"
  }

  # The state holds the runner token and the seed's contents, so it is encrypted (OpenTofu state encryption).
  encryption {
    key_provider "pbkdf2" "env" {
      passphrase = local.env["STATE_PASSPHRASE"] # at least 16 characters
    }
    method "aes_gcm" "env" {
      keys = key_provider.pbkdf2.env
    }
    state {
      method   = method.aes_gcm.env
      enforced = true
    }
  }
}

locals {
  # ci-runner/.env as a map: KEY=value lines; blank lines and "#" lines are skipped; spaces around "=", CRLF line
  # ends and surrounding quotes are stripped. A key that appears twice is an error.
  env = {
    for m in regexall("(?m)^[ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*(.*?)[ \t\r]*$", file("${path.module}/.env")) :
    m[0] => trim(m[1], "\"'")
  }

  vm_dir   = lookup(local.env, "VM_DIR", "")
  qemu_img = lookup(local.env, "QEMU_IMG", "")

  # Luna's network, READ ONLY (terraform_data.preflight checks it; nothing here creates, changes or deletes it).
  switch_name   = "LunaCI"
  nat_name      = "LunaCI-NAT"
  fw_rule       = "LunaCI-Block-VM-To-Host"
  cidr          = "192.168.250.0/24"
  prefix_len    = 24
  gateway       = "192.168.250.1"    # this PC on the LunaCI switch
  disk_bytes    = 85899345920        # 80 GiB
  gateway_allow = "192.168.250.1/32" # the one allow rule: the VM's route out (the firewall rule still blocks this PC)
  deny_ranges   = ["0.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16", "224.0.0.0/4", "240.0.0.0/4", "::/0"]

  # The repository folder (this file's parent folder), Windows style, so VM_DIR can be checked against it.
  repo_dir = replace(abspath("${path.module}/.."), "/", "\\")

  # Ubuntu 24.04 LTS generic cloud image, pinned to a dated release (SHA256SUMS in the same folder).
  image_url    = "https://cloud-images.ubuntu.com/releases/noble/release-20260926/ubuntu-24.04-server-cloudimg-amd64.img"
  image_sha256 = "6a81c37564db9b1ee84e141922625e1d7c5b389b99bb3c572e0243607d5bb4d2"

  # The ONLY settings copied into the VM. Never HYPERV_*, STATE_PASSPHRASE, VM_DIR or QEMU_IMG.
  runner_env = {
    REPO_URL     = lookup(local.env, "REPO_URL", "")
    RUNNER_TOKEN = lookup(local.env, "RUNNER_TOKEN", "")
    RUNNER_NAME  = lookup(local.env, "RUNNER_NAME", "homecontrol-ci-hyperv")
  }
  token    = local.runner_env["RUNNER_TOKEN"]
  token_ok = local.token != "" && !startswith(local.token, "paste-")
  # One VM per runner (RUNNER_COUNT in .env, 1 to 4). Runner 1 has the plain names (homecontrol-ci, IP_FIRST, disk
  # homecontrol-ci-os.vhdx, cidata\cidata.*), so adding runners never renames it (though the fresh RUNNER_TOKEN they
  # need rebuilds it); runner N is homecontrol-ci-N on 192.168.250.(IP_FIRST+N-1), RUNNER_NAME-N in GitHub. All carry
  # the label homecontrol-ci; GitHub gives each job to a free one.
  runner_count = try(tonumber(lookup(local.env, "RUNNER_COUNT", "3")), 0) # checked in preflight: a whole number, 1 to 4
  # This project's block on Luna's /24 (IP_FIRST in .env, default .30, so .30-.33 at most). Luna's own main.tf
  # hard-codes .10-.13, and the modelm runner is at .20: every address in local.taken is refused (preflight).
  ip_first = try(tonumber(lookup(local.env, "IP_FIRST", "30")), 0)
  taken    = [1, 10, 11, 12, 13, 20] # .1 this PC, .10-.13 Luna (RUNNER_COUNT up to 4), .20 modelm. Add any new VM here.
  ip_last  = local.ip_first + local.runner_count - 1
  # Memory per VM in MB (MEMORY_MB in .env, default 1024). Luna gives 4096; this project's jobs (pytest inside a
  # docker build, one Playwright browser) are lighter, and 1024 keeps three VMs at 3 GB next to Luna's. The 4 GB swap
  # file takes the peaks. Raise it if jobs get OOM-killed (exit 137) or crawl.
  memory_mb    = try(tonumber(lookup(local.env, "MEMORY_MB", "1024")), 0)
  memory_bytes = local.memory_mb * 1048576
  runners = {
    for i in range(1, local.runner_count + 1) : tostring(i) => {
      vm_name     = i == 1 ? "homecontrol-ci" : "homecontrol-ci-${i}"
      vm_ip       = "192.168.250.${local.ip_first + i - 1}"
      mac         = format("00155D4843%02X", i) # Hyper-V's 00:15:5D range, 48:43 "HC" (Luna's are 4C:43), so no MAC is ever shared on the one switch; 12 uppercase hex digits, as the provider reads it back
      runner_name = i == 1 ? local.runner_env["RUNNER_NAME"] : "${local.runner_env["RUNNER_NAME"]}-${i}"
      suffix      = i == 1 ? "" : "-${i}"
    }
  }
  # The runner-only .env written into each VM: the same, except each runner's own name.
  runner_env_files = {
    for k, r in local.runners : k => join("", [for e in sort(keys(local.runner_env)) : "${e}=${e == "RUNNER_NAME" ? r.runner_name : local.runner_env[e]}\n"])
  }

  # Docker's apt signing key, "Docker Release (CE deb)", fingerprint 9DC8 5822 9FC7 DD38 854A E2D8 8D81 803C 0EBF CD88,
  # copied from https://download.docker.com/linux/ubuntu/gpg (sha256 1500c1f56fa9e26b9b8f42452a553675796ade0807cdce11975eb98170b3a570).
  # Inline, so first boot depends on no keyserver and apt accepts only packages signed by this key.
  docker_key = <<-EOT
    -----BEGIN PGP PUBLIC KEY BLOCK-----

    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
    YT90qFF93M3v01BbxP+EIY2/9tiIPbrd
    =0YYh
    -----END PGP PUBLIC KEY BLOCK-----
  EOT

  user_data = { for k, r in local.runners : k => "#cloud-config\n${yamlencode({
    hostname      = r.vm_name
    timezone      = "Etc/UTC"
    ssh_pwauth    = false # the default user "ubuntu" gets no password and no key: nobody logs in
    growpart      = { mode = "auto", devices = ["/"] }
    resize_rootfs = true
    swap          = { filename = "/swapfile", size = "4G", maxsize = "4G" } # 1 GB RAM is tight for a docker build or Chromium
    apt = {
      sources = {
        "docker.list" = {
          source = "deb [arch=amd64 signed-by=$KEY_FILE] https://download.docker.com/linux/ubuntu $RELEASE stable"
          key    = local.docker_key
        }
      }
    }
    package_update  = true
    package_upgrade = true
    packages        = ["docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin"]
    write_files = [
      {
        # Unchanged from Luna: each setting here came from a real failure there. Docker's default networks are
        # 172.17-172.31.x and 192.168.x: move them clear of the LAN's 172.16.0.0/12 and of this VM's 192.168.250.0/24.
        path        = "/etc/docker/daemon.json"
        permissions = "0644"
        content = jsonencode({
          bip                     = "10.200.0.1/24"
          "default-address-pools" = [{ base = "10.201.0.0/16", size = 24 }]
          "log-driver"            = "local"
          # Containers ask the VM (systemd-resolved on the bridge address, below) rather than 1.1.1.1 directly:
          # the host NAT drops forwarded (NAT'd again) flows, so a bridged container's own DNS query never returns.
          dns = ["10.200.0.1"]
        })
      },
      {
        # systemd-resolved answers on the Docker bridge too (its stub listens on 127.0.0.53 only by default), so
        # containers can use the VM as their resolver. Resolution itself goes out as the VM's own traffic, which works.
        path        = "/etc/systemd/resolved.conf.d/homecontrol-ci-docker.conf"
        permissions = "0644"
        content     = "[Resolve]\nDNSStubListenerExtra=10.200.0.1\n"
      },
      {
        # 127.0.1.1 stops sudo warning that it can't resolve the hostname. (Luna's tenant*.localhost names are left
        # out: homecontrol's tests bind 127.0.0.1 by address.)
        path    = "/etc/hosts"
        append  = true
        content = "127.0.1.1 ${r.vm_name}\n"
      },
      {
        path        = "/opt/homecontrol-ci-runner/docker-compose.yml"
        owner       = "root:root"
        permissions = "0644"
        content     = file("${path.module}/docker-compose.yml")
      },
      {
        path        = "/opt/homecontrol-ci-runner/.env"
        owner       = "root:root"
        permissions = "0600"
        content     = local.runner_env_files[k]
      },
      {
        # Keeps the 80 GB disk from filling: build cache older than a week, dangling images, unused anonymous volumes.
        path        = "/etc/cron.daily/homecontrol-ci-prune"
        permissions = "0755"
        content     = "#!/bin/sh\ndocker builder prune --all --force --filter until=168h >/dev/null 2>&1\ndocker image prune --force >/dev/null 2>&1\ndocker volume prune --force >/dev/null 2>&1\nexit 0\n"
      },
      {
        # Starts the runner: `docker compose up` (an ~850 MB image pull the first time), retried every minute until it
        # succeeds, and again on every boot. A Docker Hub or network blip therefore can't leave the VM without a
        # runner. (Within the first hour, because the registration token expires; after that the registration is
        # saved and no token is needed.)
        path        = "/etc/systemd/system/homecontrol-ci-compose.service"
        permissions = "0644"
        content     = <<-EOT
          [Unit]
          Description=homecontrol CI: start the GitHub Actions runner (docker compose up), retried until it works
          Requires=docker.service
          Wants=network-online.target
          After=docker.service network-online.target
          StartLimitIntervalSec=0

          [Service]
          Type=oneshot
          RemainAfterExit=yes
          ExecStart=/usr/bin/docker compose --project-directory /opt/homecontrol-ci-runner up --detach
          Restart=on-failure
          RestartSec=60

          [Install]
          WantedBy=multi-user.target
        EOT
      },
    ]
    # runcmd runs at the end of first boot, after the packages are installed. --no-block: cloud-init finishes while
    # the unit pulls the image in the background (journalctl -u homecontrol-ci-compose).
    runcmd = [
      ["systemctl", "daemon-reload"],
      ["systemctl", "restart", "systemd-resolved"],
      ["systemctl", "enable", "docker.service", "homecontrol-ci-compose.service"],
      ["systemctl", "start", "--no-block", "docker.service", "homecontrol-ci-compose.service"],
    ]
    final_message = "${r.vm_name}: cloud-init finished after $UPTIME seconds"
  })}" }

  network_config = { for k, r in local.runners : k => yamlencode({
    network = {
      version = 2
      ethernets = {
        eth0 = {
          match       = { macaddress = join(":", regexall("..", lower(r.mac))) }
          set-name    = "eth0"
          dhcp4       = false
          dhcp6       = false
          addresses   = ["${r.vm_ip}/${local.prefix_len}"]
          routes      = [{ to = "default", via = local.gateway }]
          nameservers = { addresses = ["1.1.1.1", "9.9.9.9"] }
        }
      }
    }
  }) }

  meta_data = { for k, r in local.runners : k => "instance-id: ${r.vm_name}-${substr(sha256("${local.user_data[k]}${local.network_config[k]}"), 0, 12)}\nlocal-hostname: ${r.vm_name}\n" }

  # Windows PowerShell 5.1, which every Windows 11 has. The scripts read their values from $env:HC_* (set by
  # "environment") rather than having them pasted into the code.
  powershell = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"]
}

provider "hyperv" {
  user     = local.env["HYPERV_USER"]
  password = local.env["HYPERV_PASSWORD"]
  host     = "127.0.0.1"
  port     = 5986
  https    = true
  insecure = true # self-signed certificate on loopback
  use_ntlm = true
  timeout  = "60s"
}

# ---------------------------------------------------------------------------------------------------------------------
# Network: Luna's switch, NAT and firewall rule, checked and never touched
# ---------------------------------------------------------------------------------------------------------------------

# Luna's stack has resource "hyperv_network_switch" and terraform_data "nat" here. This stack has neither: a second
# NetNat is impossible (Windows allows one per PC, Luna's apply stops if another exists), and a second owner of the
# LunaCI switch would fight Luna's state and delete the switch on `tofu destroy` here. Instead this step only READS:
# switch, NAT prefix, gateway address and Luna's block-all-inbound firewall rule. Any missing or different and apply
# stops here, before any image, disk or VM is made. It runs again on every apply (plantimestamp), so a later apply also
# notices if Luna's network has gone. It has no destroy step: destroying this stack leaves Luna's network alone.
resource "terraform_data" "preflight" {
  triggers_replace = plantimestamp()
  input = {
    switch  = local.switch_name
    nat     = local.nat_name
    cidr    = local.cidr
    gateway = local.gateway
    fw_rule = local.fw_rule
  }

  # .env sanity checks, before anything is created (Luna has these on its switch resource).
  lifecycle {
    precondition {
      condition     = can(regex("^[A-Za-z]:\\\\[^/]*[^\\\\/]$", local.vm_dir))
      error_message = "VM_DIR in .env must be an absolute Windows path with backslashes and no trailing backslash, e.g. C:\\Hyper-V\\HomecontrolCI."
    }
    precondition {
      # The state, seed zip and ISO (all holding the runner token) must not land in the repo or a synced folder.
      condition     = !startswith(lower("${local.vm_dir}\\"), lower("${local.repo_dir}\\")) && !can(regex("(?i)(my drive|google ?drive|onedrive|dropbox)", local.vm_dir))
      error_message = "VM_DIR in .env must be outside this repository and outside Google Drive / OneDrive / Dropbox, e.g. C:\\Hyper-V\\HomecontrolCI."
    }
    precondition {
      # Separate state: sharing Luna's folder would mean sharing (and overwriting) Luna's terraform.tfstate, image and seeds.
      condition     = !can(regex("(?i)\\\\LunaCI$", local.vm_dir))
      error_message = "VM_DIR in .env must be this project's own folder (e.g. C:\\Hyper-V\\HomecontrolCI), never Luna's C:\\Hyper-V\\LunaCI."
    }
    precondition {
      condition     = length(lookup(local.env, "HYPERV_PASSWORD", "")) > 0 && !startswith(lookup(local.env, "HYPERV_PASSWORD", ""), "change-me")
      error_message = "Set HYPERV_PASSWORD in .env (the homecontrol-tofu account's password)."
    }
    precondition {
      condition     = !startswith(lookup(local.env, "STATE_PASSPHRASE", ""), "change-me")
      error_message = "Set STATE_PASSPHRASE in .env (at least 16 characters; this stack's own, not Luna's)."
    }
    precondition {
      condition     = endswith(lower(local.qemu_img), "qemu-img.exe")
      error_message = "QEMU_IMG in .env must be the full path of qemu-img.exe."
    }
    precondition {
      condition     = can(regex("^https://github\\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", local.runner_env["REPO_URL"]))
      error_message = "REPO_URL in .env must look like https://github.com/OWNER/REPO."
    }
    precondition {
      condition     = can(regex("^[A-Za-z0-9_.-]{1,64}$", local.runner_env["RUNNER_NAME"])) && !startswith(lower(local.runner_env["RUNNER_NAME"]), "luna")
      error_message = "RUNNER_NAME in .env: letters, digits, '.', '_' or '-' only, and not Luna's (luna-*)."
    }
    precondition {
      condition     = local.runner_count >= 1 && local.runner_count <= 4 && floor(local.runner_count) == local.runner_count
      error_message = "RUNNER_COUNT in .env must be 1, 2, 3 or 4."
    }
    precondition {
      condition     = local.memory_mb >= 1024 && local.memory_mb <= 8192 && local.memory_mb % 2 == 0
      error_message = "MEMORY_MB in .env must be an even number of MB from 1024 to 8192."
    }
    precondition {
      # Own block of addresses: inside .2-.254 and clear of every address already on the switch (local.taken).
      condition     = floor(local.ip_first) == local.ip_first && local.ip_first >= 2 && local.ip_last <= 254 && length(setintersection(toset(range(local.ip_first, local.ip_last + 1)), toset(local.taken))) == 0
      error_message = "IP_FIRST in .env: the VMs would use 192.168.250.${local.ip_first}-.${local.ip_last}, which overlaps an address in use on the LunaCI switch (${join(", ", [for t in local.taken : ".${t}"])}) or leaves .2-.254."
    }
    precondition {
      condition     = local.token_ok
      error_message = "Put a fresh RUNNER_TOKEN in .env (GitHub > Settings > Actions > Runners > New self-hosted runner; it lasts one hour)."
    }
  }

  # Read-only: every command below is a Get-*. Nothing is created, changed or removed.
  provisioner "local-exec" {
    interpreter = local.powershell
    environment = {
      HC_SWITCH  = self.input.switch
      HC_NAT     = self.input.nat
      HC_CIDR    = self.input.cidr
      HC_GATEWAY = self.input.gateway
      HC_FW      = self.input.fw_rule
    }
    command = <<-EOT
      $ErrorActionPreference = 'Stop'
      $alias = 'vEthernet (' + $env:HC_SWITCH + ')'
      $luna = ' It belongs to Luna''s stack (Loyalty-Rewards ci-runner/main.tf); apply that first. This stack never creates it.'
      $sw = Get-VMSwitch -Name $env:HC_SWITCH -ErrorAction SilentlyContinue
      if (-not $sw) { throw ('Hyper-V switch ' + $env:HC_SWITCH + ' not found (Get-VMSwitch).' + $luna) }
      if ("$($sw.SwitchType)" -ne 'Internal') { throw ('Switch ' + $env:HC_SWITCH + ' is ' + $sw.SwitchType + ', expected Internal. Stopping.') }
      $nat = Get-NetNat -Name $env:HC_NAT -ErrorAction SilentlyContinue
      if (-not $nat) { throw ('NAT ' + $env:HC_NAT + ' not found (Get-NetNat).' + $luna) }
      if ($nat.InternalIPInterfaceAddressPrefix -ne $env:HC_CIDR) { throw ('NAT ' + $env:HC_NAT + ' covers ' + $nat.InternalIPInterfaceAddressPrefix + ', expected ' + $env:HC_CIDR + '. Stopping.') }
      if (-not (Get-NetIPAddress -InterfaceAlias $alias -IPAddress $env:HC_GATEWAY -ErrorAction SilentlyContinue)) { throw ('This PC has no ' + $env:HC_GATEWAY + ' on ' + $alias + ': the VMs would have no gateway.' + $luna) }
      $fw = Get-NetFirewallRule -Name $env:HC_FW -ErrorAction SilentlyContinue
      if (-not $fw) { throw ('Firewall rule ' + $env:HC_FW + ' not found: nothing would stop the VMs reaching this PC.' + $luna) }
      if ("$($fw.Enabled)" -ne 'True' -or "$($fw.Action)" -ne 'Block' -or "$($fw.Direction)" -ne 'Inbound') { throw ('Firewall rule ' + $env:HC_FW + ' is not an enabled inbound Block rule. Stopping.') }
      $ifs = @($fw | Get-NetFirewallInterfaceFilter | Select-Object -ExpandProperty InterfaceAlias)
      if ($ifs -notcontains $alias) { throw ('Firewall rule ' + $env:HC_FW + ' does not cover ' + $alias + '. Stopping.') }
      Write-Output ('Using ' + $env:HC_SWITCH + ', ' + $env:HC_NAT + ' (' + $env:HC_CIDR + ') and ' + $env:HC_FW + ', read-only.')
    EOT
  }
}

# ---------------------------------------------------------------------------------------------------------------------
# Disk: pinned Ubuntu cloud image -> VHDX -> 80 GB copy
# ---------------------------------------------------------------------------------------------------------------------

# Downloads the image, refuses it unless the SHA-256 matches, and makes VM_DIR\image\base.vhdx (dynamic, 1 MB blocks
# as Microsoft recommends for Linux). Runs again only when the URL or hash above changes.
# qemu-img writes only a plain fixed VHD (raw data plus a footer); Hyper-V's own Convert-VHD writes the VHDX. qemu's
# VHDX writer is avoided: on Windows it creates NTFS sparse files, which Hyper-V refuses ("must not be sparse"), and
# by default it uses non-standard ZERO payload blocks (https://gitlab.com/qemu-project/qemu/-/issues/2204).
resource "terraform_data" "base_image" {
  depends_on = [terraform_data.preflight] # nothing is downloaded or built until Luna's network has checked out
  input = {
    dir    = local.vm_dir
    url    = local.image_url
    sha256 = local.image_sha256
  }

  provisioner "local-exec" {
    interpreter = local.powershell
    environment = {
      HC_VM_DIR       = self.input.dir
      HC_IMAGE_URL    = self.input.url
      HC_IMAGE_SHA256 = self.input.sha256
      HC_QEMU_IMG     = local.qemu_img
    }
    command = <<-EOT
      $ErrorActionPreference = 'Stop'
      $ProgressPreference = 'SilentlyContinue'
      foreach ($d in 'image', 'disk', 'cidata') { New-Item -ItemType Directory -Force -Path (Join-Path $env:HC_VM_DIR $d) | Out-Null }
      if (-not (Test-Path -LiteralPath $env:HC_QEMU_IMG)) { throw ('qemu-img not found at ' + $env:HC_QEMU_IMG + ' (QEMU_IMG in .env).') }
      $img = Join-Path $env:HC_VM_DIR 'image\ubuntu-24.04-server-cloudimg-amd64.img'
      $vhdx = Join-Path $env:HC_VM_DIR 'image\base.vhdx'
      $ok = (Test-Path -LiteralPath $img) -and ((Get-FileHash -Algorithm SHA256 -LiteralPath $img).Hash -eq $env:HC_IMAGE_SHA256)
      if (-not $ok) {
        Write-Output ('Downloading ' + $env:HC_IMAGE_URL)
        curl.exe --fail --location --retry 3 --silent --show-error --output $img $env:HC_IMAGE_URL
        if ($LASTEXITCODE -ne 0) { throw ('Download failed: ' + $env:HC_IMAGE_URL) }
        $got = (Get-FileHash -Algorithm SHA256 -LiteralPath $img).Hash
        if ($got -ne $env:HC_IMAGE_SHA256) {
          Remove-Item -LiteralPath $img -Force
          throw ('SHA-256 mismatch for the Ubuntu image: expected ' + $env:HC_IMAGE_SHA256 + ', got ' + $got + '. The file was deleted.')
        }
      }
      $tmp = Join-Path $env:HC_VM_DIR 'image\qemu-fixed.vhd'
      foreach ($f in $vhdx, $tmp) { if (Test-Path -LiteralPath $f) { Remove-Item -LiteralPath $f -Force } }
      & $env:HC_QEMU_IMG convert -f qcow2 -O vpc -o 'subformat=fixed,force_size=on' $img $tmp
      if ($LASTEXITCODE -ne 0) { throw 'qemu-img convert failed.' }
      fsutil sparse setflag $tmp 0 | Out-Null
      if ($LASTEXITCODE -ne 0) { throw ('fsutil could not clear the sparse flag on ' + $tmp) }
      Convert-VHD -Path $tmp -DestinationPath $vhdx -VHDType Dynamic -BlockSizeBytes 1MB
      Remove-Item -LiteralPath $tmp, $img -Force
      $v = Get-VHD -Path $vhdx
      if ($v.VhdFormat -ne 'VHDX' -or "$($v.VhdType)" -ne 'Dynamic') { throw ('Expected a dynamic VHDX, got ' + $v.VhdFormat + ' ' + $v.VhdType) }
      if ((Get-Item -LiteralPath $vhdx).Attributes -band [IO.FileAttributes]::SparseFile) { throw ($vhdx + ' is an NTFS sparse file, which Hyper-V refuses.') }
      Write-Output ('Base disk ready: ' + $vhdx)
    EOT
  }

  provisioner "local-exec" {
    when        = destroy
    interpreter = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"]
    environment = {
      HC_VM_DIR = self.input.dir
    }
    command = <<-EOT
      Remove-Item -LiteralPath (Join-Path $env:HC_VM_DIR 'image') -Recurse -Force -ErrorAction SilentlyContinue
      exit 0
    EOT
  }
}

# The VM's disk: a copy of base.vhdx grown to 80 GB (cloud-init's growpart then fills it). Its own folder, because
# the provider unpacks and deletes any .zip/.7z/.box it finds next to a disk. A new seed or image means a new disk.
resource "hyperv_vhd" "os" {
  for_each   = local.runners
  depends_on = [terraform_data.base_image]

  path   = "${local.vm_dir}\\disk\\${each.value.vm_name}-os.vhdx"
  source = "${local.vm_dir}\\image\\base.vhdx"
  size   = local.disk_bytes

  timeouts {
    create = "30m"
  }

  lifecycle {
    replace_triggered_by = [hyperv_iso_image.cidata[each.key], terraform_data.base_image]
  }
}

# ---------------------------------------------------------------------------------------------------------------------
# Seed: cloud-init NoCloud on an ISO labelled CIDATA
# ---------------------------------------------------------------------------------------------------------------------

data "archive_file" "cidata" {
  for_each    = local.runners
  type        = "zip"
  output_path = "${local.vm_dir}\\cidata\\cidata${each.value.suffix}.zip" # all in the one cidata folder (made before plan)

  source {
    filename = "meta-data"
    content  = local.meta_data[each.key]
  }
  source {
    filename = "network-config"
    content  = local.network_config[each.key]
  }
  source {
    filename = "user-data"
    content  = local.user_data[each.key]
  }
}

resource "hyperv_iso_image" "cidata" {
  for_each   = local.runners
  depends_on = [terraform_data.base_image] # base_image makes VM_DIR's image, disk and cidata folders

  volume_name               = "CIDATA"
  source_zip_file_path      = data.archive_file.cidata[each.key].output_path
  source_zip_file_path_hash = data.archive_file.cidata[each.key].output_sha256
  destination_zip_file_path = "${local.vm_dir}\\cidata\\cidata${each.value.suffix}-upload.zip"
  destination_iso_file_path = "${local.vm_dir}\\cidata\\cidata${each.value.suffix}.iso"
  iso_media_type            = "dvdplusrw_duallayer" # a writable type: Windows (IMAPI) refuses to write "cdrom"
  iso_file_system_type      = "iso9660|joliet"      # Joliet keeps the names user-data/meta-data; NoCloud reads iso9660
}

# ---------------------------------------------------------------------------------------------------------------------
# The VM
# ---------------------------------------------------------------------------------------------------------------------

resource "hyperv_machine_instance" "ci" {
  for_each   = local.runners
  depends_on = [terraform_data.preflight]

  name                   = each.value.vm_name
  path                   = local.vm_dir
  notes                  = "homecontrol CI self-hosted runner (homecontrol ci-runner/main.tf). Internet only; rebuilt by tofu apply. Not managed by Luna's stack."
  generation             = 2
  processor_count        = 2
  static_memory          = true # (dynamic_memory must not be set as well)
  memory_startup_bytes   = local.memory_bytes
  memory_minimum_bytes   = local.memory_bytes
  memory_maximum_bytes   = local.memory_bytes
  checkpoint_type        = "Disabled"
  automatic_start_action = "Nothing" # created NOT starting with Windows; the guard switches this to Start at the end
  automatic_start_delay  = 30
  automatic_stop_action  = "ShutDown"
  smart_paging_file_path = local.vm_dir
  snapshot_file_location = local.vm_dir

  # Created switched off; terraform_data.guard starts it once the port ACLs are on.
  state = "Off"

  lifecycle {
    ignore_changes       = [state, automatic_start_action] # both are owned by the guard after creation
    replace_triggered_by = [hyperv_iso_image.cidata[each.key], hyperv_vhd.os[each.key]]
  }

  vm_firmware {
    enable_secure_boot   = "On"
    secure_boot_template = "MicrosoftUEFICertificateAuthority" # Linux shim; the default "MicrosoftWindows" won't boot it
    boot_order {
      boot_type           = "HardDiskDrive"
      controller_number   = 0
      controller_location = 0
    }
  }

  vm_processor {
    expose_virtualization_extensions = false
  }

  integration_services = {
    "Guest Service Interface" = false # no file copy into or out of the VM
    "Heartbeat"               = true
    "Key-Value Pair Exchange" = false
    "Shutdown"                = true # clean shutdown when Windows shuts down
    "Time Synchronization"    = true
    "VSS"                     = false
  }

  network_adaptors {
    name                 = "internet"
    switch_name          = local.switch_name # Luna's switch, by name only: never managed here (preflight checked it)
    dynamic_mac_address  = false
    static_mac_address   = each.value.mac
    mac_address_spoofing = "Off"
    dhcp_guard           = "On"
    router_guard         = "Off" # On drops Docker's NAT'd (routed) traffic: containers and `docker build` got no DNS (6 Oct 2026). The ACLs and firewall rule are the wall, not this.
    wait_for_ips         = false # Hyper-V learns guest IPs through Key-Value Pair Exchange, which is off
  }

  hard_disk_drives {
    controller_type     = "Scsi"
    controller_number   = 0
    controller_location = 0
    path                = hyperv_vhd.os[each.key].path
  }

  dvd_drives {
    controller_number   = 0
    controller_location = 1
    path                = hyperv_iso_image.cidata[each.key].destination_iso_file_path
    resource_pool_name  = "Primordial" # what Hyper-V reports; the provider's default "" makes every apply try to "remove" the pool and fail
  }
}

# The wall, then the power button. Runs after every change to the VM (replacement or in-place update), fail-closed:
#   1. "start with Windows" off, and the VM switched off if it is running (an in-place update has already restarted
#      it), so nothing below ever happens while job code runs, and a failure leaves it off for good.
#   2. Makes sure the disk really is 80 GB (the provider's resize doesn't report failures).
#   3. Port ACLs: adds any missing deny rule and the gateway allow first, then removes anything not on the list, so
#      the deny list is never down; then checks every range is denied, and the gateway allowed, in both directions.
#   4. Turns the three integration services off again and re-sets the adapter guards.
#   5. Only then: "start with Windows" on, and Start-VM.
resource "terraform_data" "guard" {
  for_each = local.runners
  input = {
    vm    = hyperv_machine_instance.ci[each.key].name
    disk  = hyperv_vhd.os[each.key].path
    allow = local.gateway_allow
  }

  lifecycle {
    replace_triggered_by = [hyperv_machine_instance.ci[each.key]]
  }

  provisioner "local-exec" {
    interpreter = local.powershell
    environment = {
      HC_VM         = self.input.vm
      HC_DISK       = self.input.disk
      HC_DISK_BYTES = tostring(local.disk_bytes)
      HC_DENY       = join(",", local.deny_ranges)
      HC_ALLOW      = self.input.allow
    }
    command = <<-EOT
      $ErrorActionPreference = 'Stop'
      # Unlike the provider (WinRM as HYPERV_USER), this runs as whoever runs tofu, and the Hyper-V cmdlets need admin.
      if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run tofu from PowerShell "Run as administrator": the guard needs admin for Get-VM and the port ACLs. The VM stays off.' }
      $vm = $env:HC_VM
      if ((Get-VM -Name $vm).State -ne 'Off') { Stop-VM -Name $vm -TurnOff -Force }
      Set-VM -Name $vm -AutomaticStartAction Nothing
      $want = [int64]$env:HC_DISK_BYTES
      if ((Get-VHD -Path $env:HC_DISK).Size -lt $want) { Resize-VHD -Path $env:HC_DISK -SizeBytes $want }
      $deny = @($env:HC_DENY.Split(',') | ForEach-Object { $_.Trim().ToLowerInvariant() })
      function Get-Range([string]$Address) { return ($Address.Trim() -replace '^(?i)remote\s+', '' -replace '/32$', '').ToLowerInvariant() }
      $allow = Get-Range $env:HC_ALLOW
      function Test-Allow([string]$Dir) {
        $hit = @(Get-VMNetworkAdapterAcl -VMName $vm | Where-Object {
          "$($_.Action)" -eq 'Allow' -and (Get-Range "$($_.RemoteAddress)") -eq $allow -and @($Dir, 'Both') -contains "$($_.Direction)"
        })
        return ($hit.Count -gt 0)
      }
      function Test-Deny([string]$Net, [string]$Dir) {
        $hit = @(Get-VMNetworkAdapterAcl -VMName $vm | Where-Object {
          "$($_.Action)" -eq 'Deny' -and (Get-Range "$($_.RemoteAddress)") -eq $Net -and @($Dir, 'Both') -contains "$($_.Direction)"
        })
        return ($hit.Count -gt 0)
      }
      foreach ($net in $deny) {
        foreach ($dir in 'Inbound', 'Outbound') {
          if (-not (Test-Deny $net $dir)) { Add-VMNetworkAdapterAcl -VMName $vm -RemoteIPAddress $net -Direction $dir -Action Deny }
        }
      }
      foreach ($dir in 'Inbound', 'Outbound') {
        if (-not (Test-Allow $dir)) { Add-VMNetworkAdapterAcl -VMName $vm -RemoteIPAddress $env:HC_ALLOW -Direction $dir -Action Allow }
      }
      foreach ($a in @(Get-VMNetworkAdapterAcl -VMName $vm)) {
        $range = Get-Range "$($a.RemoteAddress)"
        $keep = ("$($a.Action)" -eq 'Deny' -and $deny -contains $range) -or ("$($a.Action)" -eq 'Allow' -and $range -eq $allow)
        if (-not $keep) { $a | Remove-VMNetworkAdapterAcl -Confirm:$false }
      }
      foreach ($net in $deny) {
        foreach ($dir in 'Inbound', 'Outbound') {
          if (-not (Test-Deny $net $dir)) { throw ('Port ACL missing: deny ' + $net + ' ' + $dir + '. The VM stays off.') }
        }
      }
      foreach ($dir in 'Inbound', 'Outbound') {
        if (-not (Test-Allow $dir)) { throw ('Port ACL missing: allow ' + $allow + ' ' + $dir + ' (the gateway). The VM stays off.') }
      }
      $off = @('Guest Service Interface', 'Key-Value Pair Exchange', 'VSS')
      Disable-VMIntegrationService -VMName $vm -Name $off
      $still = @(Get-VMIntegrationService -VMName $vm | Where-Object { $_.Enabled -and ($off -contains $_.Name) })
      if ($still.Count -gt 0) { throw ('Integration services still on: ' + (($still | Select-Object -ExpandProperty Name) -join ', ') + '. The VM stays off.') }
      Set-VMNetworkAdapter -VMName $vm -MacAddressSpoofing Off -DhcpGuard On -RouterGuard Off
      Set-VM -Name $vm -AutomaticStartAction Start
      Start-VM -Name $vm
      Write-Output ($vm + ' running behind deny ACLs for ' + ($deny -join ', ') + ', gateway ' + $allow + ' allowed.')
    EOT
  }
}

output "vm" {
  value = [for k in sort(keys(local.runners)) : "${local.runners[k].vm_name} at ${local.runners[k].vm_ip} (internet only), runner ${local.runners[k].runner_name}"]
}
