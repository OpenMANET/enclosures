# OpenChubNET Enclosure

## Overview
This enclosure is designed for OpenMANET field nodes and integrates a Pi 4B with an internal battery-backed UPS.

![Example enclosure build](pics/image.webp) 

## Order of build
1. Print files 
2. Mount the Pi4B and Waveshare D UPS to the printed mounting plate
3. Add the Li-ion battery and wiring
5. Assemble connectors to lid
4. Connect antennas
4. Install and test OpenMANET as per [instructions](https://openmanet.github.io/docs/initial-setup.html)

## Parts List

### Baseline
- 1 x Raspberry Pi 4B
- 1 x [Waveshare UPS HAT D](https://www.waveshare.com/ups-hat-d.htm)
- 2 x 21700 3.7V rechargeable lion batteries

### Connectors
- 2 x [CAZN USB-C waterproof port](https://www.aliexpress.us/item/3256806032665821.html)
- 1 x [CAZN RJ45 waterproof](https://www.aliexpress.com/item/1005006207254042.html)
- (alternative) 1 x [CAZN RJ45 waterproof 180 degrees](https://www.aliexpress.com/item/1005006207174223.html)

### Antenna cables
- 1 x [NType to UFL RG178 20cm](https://www.aliexpress.com/item/1005008877279360.html)
- 1 x [SMA to UFL 20cm](https://www.aliexpress.com/item/1005005697399884.html)

### Antennas
- 1 x [Gizont 915MHz gooseneck NType](https://www.aliexpress.com/item/1005006433957349.html)
- 1 x [Gizont GPS Stubby](https://www.aliexpress.com/item/1005006022171372.html)

## Options

### Antenna cables

I went with NType and thicker RG178 cables, but TNC or SMA options are available at resellers.

### Antennas

Gooseneck, whips, spring flexible. Check out Gizont store on Aliexpress

### Aliexpress resellers

- [Gizont Factory Store](https://www.aliexpress.com/store/1103206200)
- [CAZN Factory Store](https://www.aliexpress.com/store/1103206200)

## Print instructions

### Case

Print standing up. No supports needed

- [Case](stl/openMANET_enclosure_rpi4_ups_battery_rj45.stl)

### Mounting plate

Print laying down or at 45 degree for fewer supports. Supports needed.

- [Board](stl/openMANET_mounting_plate.stl)

### Top

Print laying down. No supports needed

- [Top](stl/openMANET_top_usbc_rj45_ntype_sma.stl)

## Reference images

![Outside view](pics/1_outside_view.png)
![Case](pics/2_case.png)
![Top](pics/3_top.png)
![Mounting plate](pics/4_mounting_plate.png)
![Assembly 1](pics/5_assembly_mp_ups_rpi_halow.png)
![Assembly 2](pics/6_assembly_case_hw.png)
