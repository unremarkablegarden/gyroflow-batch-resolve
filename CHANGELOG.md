# Changelog

All notable changes to this project are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-04

First release.

### Added

- Batch matcher: pairs fpSup gyro takes (`.GYR` + `.json`) with recorder clips and writes a `.gyroflow` beside each clip through the Gyroflow CLI, with the gyro data embedded.
- Matching by timecode when the take's `.json` has one, otherwise by cross-correlating the clip's motion with the gyro's angular rate.
- Timecode ties between takes broken by file date, then by a short decoded sample of the clip.
- Clips whose log covers at least 90% of them are placed by timecode; the seconds without gyro are reported in the status.
- GUI: clip list with subfolder paths, subfolder toggle, remove and clear, length and timecode columns, Refresh buttons.
- CLI: `gyroflow-batch`.
- GitHub workflow builds macOS, Windows and Linux on `v*` tags and publishes a Release with one zip per platform.

[Unreleased]: https://github.com/unremarkablegarden/gyroflow-batch-resolve/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/unremarkablegarden/gyroflow-batch-resolve/releases/tag/v0.1.0
