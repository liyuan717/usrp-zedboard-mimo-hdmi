# One-page English poster content

English presentation content for placement in the official AMD one-page PPT template. No team/member details are included.

## Title
Verifiable 2x2 MIMO Over-the-Air File Recovery and HDMI Playback on ZedBoard

## Motivation
Make a heterogeneous SDR/FPGA demonstration repeatable and auditable: transmit an original MP4, recover its exact bytes on ZedBoard, and verify physical HDMI presentation.

## Platform and architecture
USRP X310/CBX -> RF antennas -> FMCOMMS3/AD9361 -> RXTH DMA -> ARM synchronization and channel estimation -> PL detection -> CRC and file recovery -> ARM decoding -> HDMI. Target: AMD Zynq-7000 XC7Z020CLG484-1; Vivado 2025.2. Implementation: RTL plus ARM C++/Python, no PYNQ claim.

## Demonstrated results
Two distinct 1024x600 clips, 25 frames each. Original files recovered byte-for-byte; 1674 / 1546 CRC-correct packets; 25 physical HDMI presentations per clip. User confirmed continuous, stable content. Measured playback: approximately 4.30 / 4.49 fps. Finite receive-then-play operation, not continuous real-time streaming.

## Implementation evidence
Routed LUT utilization: 82.30%; FF: 45.57%; BRAM: 16.07%; DSP: 18.64%. WNS: +0.160 ns; WHS: +0.010 ns. Source/asset hashes, controlled stop, failed attempts and visual observations are kept separately.

## Reusable outcomes
A local web controller, immutable run identities, reference-byte checks, pinned runtime restoration, four reusable collaboration workflows and tested evidence checkers.

## Reproduce
Use the accompanying engineering package and its readme.md.
Download fixed runtime assets, verify SHA256, restore to a new directory, build with documented dependencies, then use the board/web procedure. Final poster must be laid out in AMD's official template and printed in color on A4 if entering finals.
