# Data Encryption

OpenViking supports at-rest encryption: it encrypts files before storage and decrypts them for authorized reads. Each account uses a separate account key.

## Overview

### Why Encryption

Multiple accounts can share an AGFS instance. When encryption is enabled:

- Encrypted files require the corresponding keys to decrypt; protect keys separately from data
- Different accounts' data is encrypted with independent keys for tenant isolation
- VikingFS handles encryption and decryption during reads and writes; backend coverage depends on configuration

### Transparency

Encryption is completely transparent to users and developers:

- **No client API changes**: Existing code works without modification
- **Application layer unaware**: Read/write operations behave exactly like unencrypted
- **Compatible with existing files**: Old plaintext files remain readable; enabling encryption does not encrypt them automatically

## Three-Layer Key Architecture

OpenViking uses an Envelope Encryption architecture with a three-layer key system:

```
┌─────────────────────────────────────────────────────────┐
│  Layer 1: Root Key                                     │
│  • Global unique per OpenViking instance               │
│  • Storage: KMS service / ~/.openviking/master.key    │
│  • Purpose: Derive all account keys                    │
└────────────────────┬────────────────────────────────────┘
                     │ HKDF derivation
                     ▼
┌─────────────────────────────────────────────────────────┐
│  Layer 2: Account Key (KEK)                           │
│  • One independent key per account                     │
│  • Not stored, derived at runtime                      │
│  • Purpose: Encrypt all file keys for this account     │
└────────────────────┬────────────────────────────────────┘
                     │ AES-256-GCM encryption
                     ▼
┌─────────────────────────────────────────────────────────┐
│  Layer 3: File Key (DEK)                              │
│  • New random key generated per write operation        │
│  • Stored encrypted in file header (envelope)          │
│  • Purpose: Encrypt actual file content                │
└─────────────────────────────────────────────────────────┘
```

### Key Hierarchy Summary

| Layer | Name | Description | Quantity |
|-------|------|-------------|----------|
| **Root Key** | Root Key | System master key, used to derive all account keys | 1 per instance |
| **Account Key** | Account Key | Independent key per account, derived from root key | 1 per account |
| **File Key** | File Key | One-time random key per file | 1 per write |

## Key Providers

OpenViking supports three key providers for different deployment scenarios:

| Provider | Use Case | Root Key Storage | Features |
|----------|----------|-----------------|----------|
| **Local** | Dev environments, single-node deployments | Local file `~/.openviking/master.key` | Simple, no external services |
| **Vault** | Production, multi-cloud | HashiCorp Vault Transit Engine | Enterprise-grade key management, version control |
| **Volcengine KMS** | Volcengine cloud deployments | Volcengine KMS | Cloud-native KMS service |

### Local (File)

Suitable for development and single-node deployments:

```json
{
  "encryption": {
    "enabled": true,
    "provider": "local",
    "local": {
      "key_file": "~/.openviking/master.key"
    }
  }
}
```

**Initialization command**:
```bash
ov system crypto init-key --output-file ~/.openviking/master.key
```

### Vault (HashiCorp Vault)

Suitable for production and multi-cloud deployments:

```json
{
  "encryption": {
    "enabled": true,
    "provider": "vault",
    "vault": {
      "address": "https://vault.example.com:8200",
      "token": "hvs.your-vault-token",
      "mount_point": "transit",
      "kv_mount_point": "secret",
      "kv_version": 1,
      "root_key_name": "openviking-root-key",
      "encrypted_root_key_key": "openviking-encrypted-root-key"
    }
  }
}
```

### Volcengine KMS

Suitable for Volcengine cloud deployments:

```json
{
  "encryption": {
    "enabled": true,
    "provider": "volcengine_kms",
    "volcengine_kms": {
      "key_id": "d926aa0d-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      "region": "cn-beijing",
      "access_key": "AKLTxxxxxxxxxxxxxxxxxx",
      "secret_key": "Tmpxxxxxxxxxxxxxxxxxxxxxx",
      "endpoint": null,
      "key_file": "~/.openviking/openviking-volcengine-root-key.enc"
    }
  }
}
```

## How It Works

### Write Flow

```
Client              VikingFS             FileEncryptor         KeyManager        AGFS
  │                   │                       │                     │             │
  │  write(uri, data) │                       │                     │             │
  │──────────────────>│                       │                     │             │
  │                   │  encrypt(account_id,  │                     │             │
  │                   │           plaintext)  │                     │             │
  │                   │──────────────────────>│                     │             │
  │                   │                       │ derive_account_key()│             │
  │                   │                       │────────────────────>│             │
  │                   │                       │<────────────────────│             │
  │                   │                       │  account_key        │             │
  │                   │  1. Generate random File Key                              │
  │                   │  2. Encrypt content with File Key                         │
  │                   │  3. Encrypt File Key with Account Key                     │
  │                   │  4. Build envelope format                                 │
  │                   │<──────────────────────│                     │             │
  │                   │  ciphertext           │                     │             │
  │                   │──────────────────────────────────────────────────────────>│
  │                   │                       │                     │  Write      │
  │<──────────────────│                       │                     │             │
  │   success         │                       │                     │             │
```

### Read Flow

```
Client              VikingFS             FileEncryptor         KeyManager        AGFS
  │                   │                       │                     │             │
  │  read(uri)        │                       │                     │             │
  │──────────────────>│                       │                     │             │
  │                   │──────────────────────────────────────────────────────────>│
  │                   │                       │                     │  Read       │
  │                   │<──────────────────────────────────────────────────────────│
  │                   │ raw_bytes             │                     │             │
  │                   │ Check magic == "OVE1"?│                     │             │
  │                   │ Yes → decrypt()       │                     │             │
  │                   │──────────────────────>│                     │             │
  │                   │                       │ derive_account_key()│             │
  │                   │                       │────────────────────>│             │
  │                   │                       │<────────────────────│             │
  │                   │                       │  account_key        │             │
  │                   │  1. Parse envelope format                                 │
  │                   │  2. Decrypt File Key with Account Key                     │
  │                   │  3. Decrypt content with File Key                         │
  │                   │<──────────────────────│                     │             │
  │                   │  plaintext            │                     │             │
  │<──────────────────│                       │                     │             │
  │   content         │                       │                     │             │
```

### Envelope Format

Encrypted files use a unified envelope format starting with the magic number `OVE1` (OpenViking Encryption v1):

```
┌─────────────────────────────────────────────────────────────┐
│  Magic   │ Version │ Provider  │ Encrypted File Key │  ...  │
│  4 bytes │ 1 byte  │  1 byte   │   Variable length  │  ...  │
│  "OVE1"  │  0x01   │ 0x01=local│                    │  ...  │
└─────────────────────────────────────────────────────────────┘
```

- If a file doesn't start with `OVE1`, it's treated as unencrypted and plaintext is returned directly
- Old files remain readable; protecting existing plaintext requires a separate migration or rewrite

## Multi-Tenant Isolation

Different accounts' data is encrypted with independent Account Keys:

- Account A's key cannot decrypt Account B's files
- Even with full AGFS access, data can't be read without the corresponding key
- Separate keys supplement tenant access controls; they do not replace authentication or storage permissions

## Configuration Example

See [Configuration Guide](../guides/01-configuration.md#encryption) for detailed configuration.

## Related Documentation

- [Storage Architecture](./05-storage.md) - VikingFS and AGFS architecture
- [Configuration Guide](../guides/01-configuration.md) - Encryption configuration details
- [Multi-Tenant](./11-multi-tenant.md) - Account, user, and agent isolation model
