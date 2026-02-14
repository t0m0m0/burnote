# Vendored Dependencies

All external resources are self-hosted to eliminate CDN dependencies
and supply-chain risk (see Issue #34).

## qrcode-generator v1.4.4

- **Source**: `https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/qrcode.min.js`
- **Homepage**: https://github.com/nicehash/qrcode-generator
- **License**: MIT (see `LICENSE-qrcode.txt`)
- **SHA-256**: `bb2365e4902f4f84852cf4025e6f6a60325a682aeafa43fb63b7fc8f098d1ef2`

## Inter Font

- **Source**: Google Fonts (https://fonts.google.com/specimen/Inter)
- **Homepage**: https://github.com/rsms/inter
- **License**: SIL Open Font License 1.1 (see `LICENSE-inter.txt`)
- **Subsets**: latin, latin-ext
- **Weights**: 300, 400, 500, 600, 700
- **Format**: woff2

### Font file checksums (SHA-256)

| File | Subset | SHA-256 |
|------|--------|---------|
| `UcC73FwrK3iLTeHuS_nVMrMxCp50SjIa1ZL7.woff2` | latin | `3100e775e8616cd2611beecfa23a4263d7037586789b43f035236a2e6fbd4c62` |
| `UcC73FwrK3iLTeHuS_nVMrMxCp50SjIa25L7SUc.woff2` | latin-ext | `34b9c504cab7a73e37b746343a449132e56cf7b5481af2cb81dc74dcff25c956` |

## Updating

When updating vendored files, regenerate checksums with:

```sh
sha256sum static/vendor/qrcode.min.js static/vendor/fonts/*.woff2
```
