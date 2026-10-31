# V2 connection and migration index

[Documentation index](../README.md) · [English runtime overview](../runtime-baseline.md)

## Current configuration references

1. Start with the [independent runtime baseline](../runtime-baseline.zh-CN.md) ([English overview](../runtime-baseline.md)). Quant owns its runtime; do not install a cross-owner core from the old wrapper design.
2. Follow [worker project/owner scoping](../worker-project-scoping.md), including the explicit service-owner configuration for 2.0.9 and later. Projects and owner-wide grants have distinct version-specific rules.
3. Use [scenario contracts](../scenario-contracts.md) for offline task templates and producer-bound inputs. Configure a separately approved HTTPS/identity/provider connection; source changes or examples do not create one.

## Migration history

The dated `quant.zh-CN.md`, `identity-inventory.zh-CN.md` and `retired-v1.zh-CN.md` files retain migration, login-inventory and retirement context. They contain version-specific or environment-specific statements and are not the current connection guide or proof of current login/deployment. In particular, the former shared-core/thin-wrapper design was superseded by the independent runtime baseline.

V1 gateway components and fixed business orchestration are retired from active source. The old Cloudflare dashboard is not a V2 API entry. Business prompts, validation, adoption and financial permissions remain with the owning business repository. This index adds no authority to restore old workflows, merge, deploy, or submit business tasks.
