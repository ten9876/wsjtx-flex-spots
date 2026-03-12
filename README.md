# WSJT-X to Flex Spots Bridge

**Version:** 1.3 (2025-03-11)

Python script that listens to WSJT-X multicast UDP decodes and injects them as colored spots on a FlexRadio panadapter (SmartSDR API).

## Features
- Red spots when someone calls **your callsign**
- Green spots for CQ POTA decodes
- Time-based deduplication & refresh matching your chosen spot lifetime
- Interactive setup: callsign, filter mode (CQ / CQ POTA / none), lifetime seconds

## Requirements
- Python 3.6+
- WSJT-X (with UDP multicast enabled: 224.0.0.1 port 2237)
- FlexRadio 6000/8000 series with SmartSDR API enabled (port 4992)

## Usage
1. Run the script:
   ```bash
   python3 wsjtx_to_flex_spots.py

2. Follow prompts:
Enter your callsign
Choose filter (1 = CQ only, 2 = CQ POTA only, 3 = no filter)
Choose spot lifetime (60–600 seconds)


Spots appear on your Flex panadapter (Maestro/SmartSDR) with color.

## License

MIT License — feel free to use, modify, share.

## Contributing

Pull requests welcome! Especially improvements to callsign parsing, color schemes, or additional filters.

