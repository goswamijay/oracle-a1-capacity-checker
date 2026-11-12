# Oracle Cloud A1 Flex Automatic Capacity Checker & VM Creator

An enterprise-grade, non-destructive automation system designed to continuously monitor Oracle Cloud Infrastructure (OCI) for available `VM.Standard.A1.Flex` (ARM Ampere) capacity in Mumbai (`ap-mumbai-1`) and automatically launch your instance the instant capacity opens up.

---

## Architecture Overview

```
                          ┌─────────────────────────────────────┐
                          │         GitHub Actions / VPS        │
                          │   (Scheduled / Daemon Automation)   │
                          └──────────────────┬──────────────────┘
                                             │
                                    1. Authenticate (API Key)
                                             │
                                             ▼
                          ┌─────────────────────────────────────┐
                          │   Pre-Flight Safety Validations     │
                          │  - Check duplicate instance exists  │
                          │  - Verify boot volume state & AD    │
                          │  - Verify boot volume NOT attached  │
                          │  - Verify subnet state & AD match   │
                          └──────────────────┬──────────────────┘
                                             │
                                   Checks Pass? (Zero Data Loss)
                                             │
                        ┌────────────────────┴────────────────────┐
                        ▼                                         ▼
            [Out of Capacity Error]                     [Launch Accepted!]
                        │                                         │
                        ▼                                         ▼
            Retry on Next Cycle                         Record OCID to Disk
           (Exponential Backoff)                      Send Telegram Notification
                                                      Auto-Disable GH Action
```

---

## Target Specifications

- **Tenancy:** `hijay2002`
- **Compartment:** `hijay2002` (root compartment)
- **Region:** India West (Mumbai) — `ap-mumbai-1`
- **Target Availability Domain:** AD-1 (e.g. `kONR:AP-MUMBAI-1-AD-1`)
- **Instance Display Name:** `kron_new`
- **Shape:** `VM.Standard.A1.Flex`
- **Specs:** 2 OCPU, 12 GB RAM
- **Boot Volume Source:** Existing boot volume named `kron-ai` (Preserved intact)
- **Subnet:** `public-subnet-v1_kron` (Public IPv4 enabled)

---

## Table of Contents

1. [Step 1: Create a Private GitHub Repository](#step-1-create-a-private-github-repository)
2. [Step 2: Generate and Configure OCI API Signing Keys](#step-2-generate-and-configure-oci-api-signing-keys)
3. [Step 3: Locate Tenancy, User, and Compartment OCIDs](#step-3-locate-tenancy-user-and-compartment-ocids)
4. [Step 4: Find the Full Availability Domain Name](#step-4-find-the-full-availability-domain-name)
5. [Step 5: Find the Boot Volume OCID (kron-ai)](#step-5-find-the-boot-volume-ocid-kron-ai)
6. [Step 6: Verify Boot Volume Attachment Status](#step-6-verify-boot-volume-attachment-status)
7. [Step 7: Find the Subnet OCID (public-subnet-v1_kron)](#step-7-find-the-subnet-ocid-public-subnet-v1_kron)
8. [Step 8: Configure GitHub Repository Secrets](#step-8-configure-github-repository-secrets)
9. [Step 9: Upload Project Files to GitHub](#step-9-upload-project-files-to-github)
10. [Step 10: Run the Automation Manually](#step-10-run-the-automation-manually)
11. [Step 11: Inspect Execution Logs & Step Summary](#step-11-inspect-execution-logs--step-summary)
12. [Step 12: Understanding Error Classifications](#step-12-understanding-error-classifications)
13. [Step 13: Understanding Successful Creation](#step-13-understanding-successful-creation)
14. [Step 14: Stopping or Disabling the Automation](#step-14-stopping-or-disabling-the-automation)
15. [Step 15: Optional VPS Continuous 60-Second Checker](#step-15-optional-vps-continuous-60-second-checker)

---

## Step 1: Create a Private GitHub Repository

> [!CRITICAL]
> **Always make your repository PRIVATE.** Never use a public repository when working with cloud infrastructure automation.

1. Navigate to [github.com/new](https://github.com/new).
2. Enter a repository name, e.g. `oracle-a1-capacity-checker`.
3. Set the visibility to **Private**.
4. Leave "Add a README" unchecked (we will push the prepared project directly).
5. Click **Create repository**.

---

## Step 2: Generate and Configure OCI API Signing Keys

OCI uses RSA API key pairs to authenticate API calls.

### Option A: Let Oracle Generate the Key Pair (Easiest)
1. Log in to the [Oracle Cloud Console](https://cloud.oracle.com/).
2. In the top-right corner, click on the **Profile icon (User icon)** -> Click on your username / email (e.g. `hijay2002`).
3. On the left side under **Resources**, click **API Keys**.
4. Click **Add API Key**.
5. Select **Generate API Key Pair**.
6. Click **Download Private Key** (saves as `oci_api_key.pem`).
7. Click **Add**.
8. A dialog titled **Configuration File Preview** will appear. Copy the `fingerprint`, `user`, and `tenancy` shown on this screen, or copy them in Step 3.

### Option B: Generate Locally via OpenSSL
If you prefer generating the key on your local machine:
```bash
# Generate private key (PEM 2048-bit)
openssl genrsa -out ~/.oci/oci_api_key.pem 2048
chmod 600 ~/.oci/oci_api_key.pem

# Extract public key
openssl rsa -pubout -in ~/.oci/oci_api_key.pem -out ~/.oci/oci_api_key_public.pem

# In OCI Console -> User Settings -> API Keys -> Add API Key -> Paste Public Key
```

---

## Step 3: Locate Tenancy, User, and Compartment OCIDs

Every resource in OCI has an **OCID** (Oracle Cloud Identifier) formatted like `ocid1.<type>.oc1..<unique_id>`.

1. **Tenancy OCID:**
   - Click the Profile icon in the top right -> Click **Tenancy: hijay2002**.
   - Under **Tenancy Information**, click **Copy** next to **OCID**.
2. **User OCID:**
   - Click the Profile icon -> Click your username under **User Settings**.
   - Under **User Information**, click **Copy** next to **OCID**.
3. **Compartment OCID:**
   - Click the Hamburger menu (☰) -> **Identity & Security** -> **Compartments**.
   - Since your root compartment is `hijay2002`, the Compartment OCID is identical to your Tenancy OCID.

---

## Step 4: Find the Full Availability Domain Name

In Mumbai (`ap-mumbai-1`), there is 1 Availability Domain (AD-1). The full OCI internal name has a tenant-specific prefix, e.g. `kONR:AP-MUMBAI-1-AD-1`.

> [!NOTE]
> The automation code in `src/instance_manager.py` includes **automatic fuzzy AD resolution**. If you provide `AD-1`, the script automatically queries your tenancy via the OCI Identity API and matches it to the exact full AD name.

To verify your exact AD name in the console:
1. Open the OCI Cloud Shell (icon `>_` at top right of the console) or run via OCI CLI:
   ```bash
   oci iam availability-domain list
   ```
2. Note the `"name"` field (e.g. `kONR:AP-MUMBAI-1-AD-1`).

---

## Step 5: Find the Boot Volume OCID (kron-ai)

> [!IMPORTANT]
> The script launches from your **existing boot volume** `kron-ai` using `InstanceSourceViaBootVolumeDetails`. No new empty image will be launched, preserving your data completely.

1. In the OCI Console, open the Hamburger Menu (☰) -> **Storage** -> **Boot Volumes**.
2. Ensure you are in the **ap-mumbai-1** region and **hijay2002** compartment.
3. Locate the boot volume named **`kron-ai`**.
4. Click on **kron-ai** to open its details page.
5. Beside **OCID**, click **Copy**. (It starts with `ocid1.bootvolume.oc1.ap-mumbai-1...`).

---

## Step 6: Verify Boot Volume Attachment Status

OCI rules strictly dictate that a boot volume can only boot an instance if it is **not already attached** to another instance.

1. On the `kron-ai` Boot Volume details page:
2. Check the **State**: It must say **Available** (green badge).
3. Under **Attached Instances** (on the left menu):
   - If an old instance is listed, check its state. If the instance was terminated, the boot volume will detach automatically.
   - If the volume shows **Attached**, the automation will detect this and **halt immediately** without modifying anything to prevent corruption.
   - Detach the old instance manually in the console if necessary.

---

## Step 7: Find the Subnet OCID (public-subnet-v1_kron)

1. Open Hamburger Menu (☰) -> **Networking** -> **Virtual Cloud Networks**.
2. Click on your VCN.
3. Under **Subnets**, locate **`public-subnet-v1_kron`**.
4. Click on `public-subnet-v1_kron`.
5. Beside **OCID**, click **Copy**. (It starts with `ocid1.subnet.oc1.ap-mumbai-1...`).

---

## Step 8: Configure GitHub Repository Secrets

In your private GitHub repository:
1. Go to **Settings** -> **Secrets and variables** -> **Actions**.
2. Click **New repository secret** for each of the following:

| Secret Name | Value Description | Example / Format |
|---|---|---|
| `OCI_TENANCY` | Tenancy OCID | `ocid1.tenancy.oc1..aaaa...` |
| `OCI_USER` | User OCID | `ocid1.user.oc1..aaaa...` |
| `OCI_FINGERPRINT` | API Key Fingerprint | `20:3b:97:11:22:...` |
| `OCI_REGION` | Region Identifier | `ap-mumbai-1` |
| `OCI_PRIVATE_KEY` | Entire content of `oci_api_key.pem` | `-----BEGIN RSA PRIVATE KEY-----...-----END RSA PRIVATE KEY-----` |
| `OCI_COMPARTMENT` | Compartment OCID (Root) | `ocid1.tenancy.oc1..aaaa...` |
| `OCI_AD` | Availability Domain | `AD-1` (or full name `kONR:AP-MUMBAI-1-AD-1`) |
| `OCI_SUBNET` | Subnet OCID (`public-subnet-v1_kron`) | `ocid1.subnet.oc1.ap-mumbai-1..aaaa...` |
| `OCI_BOOT_VOLUME` | Boot Volume OCID (`kron-ai`) | `ocid1.bootvolume.oc1.ap-mumbai-1..aaaa...` |
| `TELEGRAM_BOT_TOKEN` *(Optional)* | Telegram Bot Token from `@BotFather` | `123456789:ABCdef...` |
| `TELEGRAM_CHAT_ID` *(Optional)* | Your numeric Telegram Chat ID | `987654321` |

> [!TIP]
> When pasting `OCI_PRIVATE_KEY`, copy the **entire file content** including the `-----BEGIN RSA PRIVATE KEY-----` and `-----END RSA PRIVATE KEY-----` header and footer lines.

---

## Step 9: Upload Project Files to GitHub

From the `oracle-a1-capacity-checker` directory on your machine:

```bash
git init
git add .
git commit -m "feat: Initial commit for Oracle A1 Flex capacity automation"
git branch -M main
git remote add origin https://github.com/<YOUR_GITHUB_USERNAME>/<YOUR_REPO_NAME>.git
git push -u origin main
```

---

## Step 10: Run the Automation Manually

1. Go to the **Actions** tab in your GitHub repository.
2. Under "All workflows", click **Oracle Cloud A1 Flex Capacity Checker & VM Creator**.
3. On the right, click **Run workflow** -> Select `Branch: main` -> Click **Run workflow**.

The workflow will also run automatically every **5 minutes** on GitHub's schedule (`cron: '*/5 * * * *'`).

---

## Step 11: Inspect Execution Logs & Step Summary

Click on any running or completed workflow run in the **Actions** tab:

1. Click on the job **Check A1 Flex Capacity & Create VM**.
2. Click on **Execute Capacity Check & Instance Creator** to view the real-time log.
3. You will see structured, safe logs:
   - Config validation (all secrets masked).
   - Pre-flight checks: AD resolution, boot volume detachment check, subnet check.
   - Attempt count and elapsed time.
   - Authoritative launch response from OCI.

---

## Step 12: Understanding Error Classifications

The checker intelligently classifies every response:

- **`CAPACITY` (Retryable):**
  OCI returns `"Out of capacity for shape VM.Standard.A1.Flex in availability domain AD-1"`. The tool logs a retry notice and waits for the next attempt.
- **`AUTHENTICATION` (Fatal):**
  401 / 403 Invalid signature or credentials. The script halts immediately and sends an alert.
- **`CONFIGURATION` (Fatal):**
  Subnet not found, boot volume attached to another VM, or quota exceeded. The script halts immediately to prevent infinite erroneous retries.
- **`RATE_LIMIT` / `TRANSIENT` (Retryable):**
  OCI 429 or 5xx temporary network blip. The script applies exponential backoff and continues.

---

## Step 13: Understanding Successful Creation

When Oracle has available capacity:
1. OCI accepts the launch request and enters `PROVISIONING` state.
2. The automation logs:
   ```
   ================================================================
   🎉 SUCCESS! INSTANCE CREATED AUTOMATICALLY BY OCI!
   Instance Display Name : kron_new
   Instance OCID         : ocid1.instance.oc1.ap-mumbai-1...
   Lifecycle State       : PROVISIONING
   Shape                 : VM.Standard.A1.Flex
   ================================================================
   ```
3. A JSON state file `launch_state.json` is created.
4. If Telegram is configured, you receive an instant alert on your phone.
5. In GitHub Actions, the workflow step summary displays the instance details and uses `gh workflow disable` to **automatically disable future scheduled runs**, saving your runner minutes!

---

## Step 14: Stopping or Disabling the Automation

- **Automatic:** The workflow automatically disables its schedule once the instance is created or if `kron_new` is detected as already active.
- **Manual:** In the GitHub repository, click **Actions** -> click **Oracle Cloud A1 Flex Capacity Checker & VM Creator** -> click the `...` (options) icon on the top right -> click **Disable workflow**.

---

## Step 15: Optional VPS Continuous 60-Second Checker

If you have an existing small VPS (or want to run locally on your PC / Raspberry Pi) for guaranteed 60-second polling:

### Setup on Linux VPS:
1. Clone the repository to `/opt/oracle-a1-capacity-checker`:
   ```bash
   sudo git clone https://github.com/<YOUR_USER>/oracle-a1-capacity-checker.git /opt/oracle-a1-capacity-checker
   cd /opt/oracle-a1-capacity-checker
   ```
2. Create python virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill in your values:
   ```bash
   cp .env.example .env
   nano .env
   ```
4. Install and enable the systemd service:
   ```bash
   sudo cp systemd/oci-checker.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now oci-checker.service
   ```
5. Check real-time logs:
   ```bash
   journalctl -u oci-checker.service -f
   ```

When the instance is launched, the service completes and exits cleanly!

---

## License

MIT License. See [LICENSE](LICENSE) for details.
