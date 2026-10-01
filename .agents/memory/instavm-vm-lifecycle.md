---
name: InstaVM VM lifecycle
description: Non-obvious terminal VM states returned by InstaVM.
---

InstaVM can return `killed` as a VM status. A killed VM is terminal and must not be reused for later sandbox calls.

**Why:** An account-level status check returned both `active` and `killed` VMs; the earlier reuse logic only recognized `terminated`, `deleted`, `failed`, `expired`, and `stopped`.

**How to apply:** When reusing a persisted InstaVM ID, treat `killed` and other unavailable/terminal states as stale and create a replacement VM.