# Security

ClearSpace is a local Windows program. It does not open a network port, does not have an account, and does not send files or telemetry anywhere. That does **not** make an installed copy safe against every threat. Read this before installing.

## Safe way to install

1. Download the zip **and** `SHA256SUMS` from the Releases page of this repository only. Do not use forks, reposts, or a random `ClearSpace.exe`.
2. Check the hash before unzipping:

```powershell
powershell -File scripts\verify_release.ps1 -Zip .\ClearSpace-2.0.0-win64.zip -Sums .\SHA256SUMS
```

3. If the hash does not match, delete the zip. Do not run it.
4. Windows SmartScreen may warn because the executable is not Authenticode-signed. Do not disable SmartScreen to get past the warning. A matching hash means the file is the one built by this repository's GitHub Actions workflow from the tagged source.

Building it yourself from a tagged commit is the other safe option: `build_dist.bat` installs only the hashed packages in `requirements.lock`.

## What the program can and cannot do

- It can read the files your Windows user can read, and it can move files you confirm to the Recycle Bin. That is the job of a disk cleaner.
- It cannot be contacted from the internet. Nothing in `app/` opens a connection.
- Duplicate checking hashes file contents in memory and discards them. The local cache stores paths, sizes and dates, not file contents, and only your Windows account can read it.
- It does not update itself. A new version never downloads in the background.

## What is still possible

- Malware or a person who already controls the PC can read your files with or without ClearSpace.
- A Windows administrator on the same PC can read the local cache.
- A fake website can offer a tampered zip. The hash check above is what stops that.
- The GitHub Actions build uses the pinned dependencies in `requirements.lock`. A compromised PyPI package that does not match those hashes is rejected.

## Reporting

Open a private security advisory on this repository, or an issue if the problem is already public. Do not paste personal file paths into a public issue.
