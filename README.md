# RPi Zero Traffic Monitor

A Raspberry Pi Zero 2 W project that displays real-time traffic and ETA information on an SSD1305 OLED display using the Google Maps Routes API.

## Features

- Real-time traffic-aware route calculations
- ETA display updated every 2 minutes
- Traffic condition indicator (Normal/Medium/Heavy)
- Runs only during weekday daytime hours (7am-4pm UK time)
- Low-power OLED display (SSD1305 128x32)

## Hardware

- Raspberry Pi Zero 2 W
- SSD1305 128x32 OLED display (I2C)

## Installation

1. Clone the repository:
   ```bash
   git clone git@github.com:matthew-brough/rpi-zero-traffic-monitor.git
   cd rpi-zero-traffic-monitor
   ```

2. Create and activate a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Copy and configure the example files:
   ```bash
   cp gmap_tracker/headers.example.json gmap_tracker/headers.json
   cp gmap_tracker/locations.example.json gmap_tracker/locations.json
   ```

5. Edit `gmap_tracker/headers.json` with your Google Maps API key:
   ```json
   {
     "X-Goog-Api-Key": "YOUR_GOOGLE_MAPS_API_KEY"
   }
   ```

6. Edit `gmap_tracker/locations.json` with your origin, destination, and optional intermediate waypoints.

## Usage

```bash
python gmap_tracker/tracking.py
```

The display will show:
- **ETA**: Estimated arrival time
- **Travel time**: Duration in minutes
- **Traffic**: Current traffic conditions

## Google Maps API Setup

1. Create a project in [Google Cloud Console](https://console.cloud.google.com/)
2. Enable the **Routes API**
3. Create an API key and restrict it to the Routes API
4. Add the key to `gmap_tracker/headers.json`

## License

GPLv3
