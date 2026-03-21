# Smart Bro GreenPacket D2-220G Manager

A local web-based management tool for the **GreenPacket D2-220G** LTE router (used by Smart Bro Home WiFi). Runs entirely on your local network — no data leaves your home.

Built with Python + Flask, accessible via browser at `http://localhost:5000`.

---

## Features

- **Signal Statistics** — Live RSRP, RSRQ, RSSI, SINR, Band, and Cell ID readout
- **LTE Band Locking** — Lock the modem to specific LTE bands (B1, B3, B5, B7, B8, B28, B40, B41)
- **Quick Presets** — One-click band combinations (B28 Only, B1+B3 CA, B1+B3+B28 triple CA, etc.)
- **IMEI Changer** — Change the modem IMEI with preset lists for Globe At Home and SmartBro/PLDT
- **WiFi Band Control** — Enable or disable the 2.4 GHz and 5 GHz radios independently
- **AT / Shell Terminal** — Send raw AT commands or shell commands directly to the router

---

## Installation

### 1. Install Python

Make sure Python 3 is installed on your machine.

- **Windows** — Download from [python.org](https://www.python.org/downloads/). During install, check **"Add Python to PATH"**
- **macOS** — Run `brew install python` (requires Homebrew), or download from python.org
- **Linux** — Usually pre-installed. If not: `sudo apt install python3 python3-pip`

Verify your install:

```bash
python3 --version
```

### 2. Install Flask

```bash
pip install flask
```

Or if `pip` isn't found:

```bash
pip3 install flask
```

### 3. Download the Script

Save `d2_manager.py` to any folder on your computer (e.g. your Desktop).

---

## Running the Tool

```bash
python3 d2_manager.py
```

Then open your browser and go to:

```
http://localhost:5000
```

> Make sure your computer is connected to the D2-220G router's WiFi (or via LAN cable) before running.

---

## One-Time Setup — Enable Telnet Access

Telnet access must be enabled once after every router reboot. It resets when the router is powered off.

1. Open [http://192.168.1.1](http://192.168.1.1) or [http://smartbrosettings.net](http://smartbrosettings.net)
2. Go to **Advanced Settings → System → Diagnostics** (Ping Test)
3. In the **ping host** field, paste exactly:
   ```
   127.0.0.1 & busybox telnetd -p 2323 -l /bin/sh
   ```
4. Click **Ping** or **Start** — the page may show an error, that's normal
5. Open the manager and click **Check Connection**

The tool connects to the router via telnet on port `2323` at `192.168.1.1`.

---

## How It Works — Architecture Overview

```mermaid
flowchart LR
    Browser["Browser\nhttp://localhost:5000"]
    Flask["Flask Server\nd2_manager.py"]
    Telnet["Telnet\n192.168.1.1:2323"]
    Modem["LTE Modem\n/dev/ttyUSB2"]
    Tower["Cell Tower\n(Smart Bro)"]

    Browser -- "HTTP REST API" --> Flask
    Flask -- "TCP Socket" --> Telnet
    Telnet -- "AT Commands\nvia ubus / at.sh" --> Modem
    Modem -- "LTE Signal" --> Tower
```

---

## User Story Diagrams

### First-Time Setup Flow

```mermaid
flowchart TD
    A([User opens router admin\n192.168.1.1]) --> B[Go to Advanced Settings\n→ System → Diagnostics]
    B --> C[Paste telnet unlock command\nin ping host field]
    C --> D[Click Ping / Start]
    D --> E{Telnet port 2323\nreachable?}
    E -- No --> C
    E -- Yes --> F[Run python3 d2_manager.py]
    F --> G[Open browser\nlocalhost:5000]
    G --> H([Tool is ready to use])
```

### Signal Check

```mermaid
sequenceDiagram
    actor User
    participant UI as Browser UI
    participant Flask as Flask Server
    participant Router as Router via Telnet

    User->>UI: Click Refresh
    UI->>Flask: GET /api/signal
    Flask->>Router: AT+CESQ
    Flask->>Router: AT+ZNLOCKBAND?
    Flask->>Router: AT+CEREG?
    Flask->>Router: AT+ECSQ
    Router-->>Flask: Raw AT responses
    Flask-->>UI: RSRP, RSRQ, Band, Cell ID
    UI-->>User: Signal stats displayed
```

### LTE Band Locking Flow

```mermaid
flowchart TD
    A([User opens Band Locking]) --> B{How to select?}
    B -- Manual --> C[Click one or more\nband buttons]
    B -- Preset --> D[Click a Quick Preset\ne.g. B28 Only]
    C --> E[Click Apply Band Lock]
    D --> E
    E --> F[Flask sends\nAT+ZNLOCKBAND command]
    F --> G[Modem restarts\nAT+CFUN=1,1]
    G --> H[Wait ~15 seconds]
    H --> I([Modem reconnects\non selected band])

    A --> J[Click Auto All Bands]
    J --> K[Flask sends AT+ZNLOCKBAND=0]
    K --> G
```

### IMEI Change Flow

```mermaid
sequenceDiagram
    actor User
    participant UI as Browser UI
    participant Flask as Flask Server
    participant Router as Router via Telnet
    participant Modem as LTE Modem

    User->>UI: Select preset IMEI or type custom
    User->>UI: Click Apply IMEI
    UI->>Flask: POST /api/change-imei {imei}
    Flask->>Flask: Validate 15-digit format
    Flask->>Router: AT*PROD=2 (enable production mode)
    Router-->>Flask: OK
    Flask->>Router: AT*MRD_IMEI=D (clear IMEI)
    Router-->>Flask: OK
    Flask->>Modem: at+egmr=1,7,"IMEI" via /dev/ttyUSB2
    Modem-->>Flask: OK
    Flask->>Router: AT*PROD=0 (disable production mode)
    Router-->>Flask: OK
    Flask-->>UI: Step-by-step result
    UI-->>User: Done — reboot modem to apply
```

### WiFi Control Flow

```mermaid
flowchart LR
    A([User]) --> B{Select action}
    B --> C[Enable 2.4 GHz]
    B --> D[Disable 2.4 GHz]
    B --> E[Enable 5 GHz]
    B --> F[Disable 5 GHz]
    B --> G[Check Status]

    C --> H[uci set radio0.disabled=0\n+ wifi restart]
    D --> I[uci set radio0.disabled=1\n+ wifi restart]
    E --> J[uci set radio1.disabled=0\n+ wifi restart]
    F --> K[uci set radio1.disabled=1\n+ wifi restart]
    G --> L[uci show wireless]

    H & I & J & K & L --> M([Result shown in output box])
```

---

## How to Use

### 1. Signal Statistics
- Click **Refresh** to pull live signal data from the modem
- Displays RSRP, RSRQ, RSSI, SINR, current Band, and Cell ID
- Raw AT command output is shown below for detailed inspection

### 2. LTE Band Locking
- Click one or more band buttons to select them (they highlight green)
- Click **Apply Band Lock** — the modem will restart and reconnect on the selected band(s)
- Click **Auto (All Bands)** to remove the lock and let the modem pick automatically
- Selecting multiple bands enables **Carrier Aggregation (CA)** for higher speeds

### 3. Quick Presets
- Click any preset card to instantly lock to that band combination
- No need to manually select bands — it applies immediately
- Good starting points: **B28 Only** for best range, **B1+B3 CA** for fastest urban speeds

### 4. IMEI Changer
- **From preset** — click any IMEI from the Globe or Smart list; it fills the input field
- **Custom** — type your own 15-digit IMEI directly into the input field
- Click **Apply IMEI** to run the change sequence
- Reboot the router/modem after applying for the new IMEI to take effect

### 5. WiFi Band Control
- Click **Enable / Disable** under **2.4 GHz Radio** or **5 GHz Radio** to toggle each independently
- Click **Check WiFi Status** to view the current wireless configuration

### 6. AT / Shell Terminal
- Type any AT command (e.g. `AT+ZNLOCKBAND?`) or shell command (e.g. `cat /proc/cpuinfo`) and press **Send** or hit Enter
- Commands starting with `AT` are routed through the modem daemon; all others run as shell
- Use the quick-access buttons for common commands: Current Band Lock, Signal Quality, Registration, etc.

---

## LTE Band Reference

| Band | Frequency     | Notes                        |
|------|---------------|------------------------------|
| B1   | 2100 MHz      | Common urban coverage        |
| B3   | 1800 MHz      | Good speed in dense areas    |
| B5   | 850 MHz       | Better wall penetration      |
| B7   | 2600 MHz      | Fast but short range         |
| B8   | 900 MHz       | Wide coverage                |
| B28  | 700 MHz APT   | Best range and indoor signal |
| B40  | TDD 2300 MHz  | Smart Bro TDD band           |
| B41  | TDD 2500 MHz  | Smart Bro TDD band           |

Multiple bands can be selected at once to enable **Carrier Aggregation (CA)** for faster speeds.

---

## IMEI Changer

Changes the modem IMEI to unlock carrier-specific promos:

- **Globe At Home Prepaid WiFi** — Fam Surf, Home Surf
- **SmartBro Home WiFi / PLDT Prepaid WiFi / Rocket SIM** — Unli Data 599, Unli Fam, Fam Load

You can enter a custom 15-digit IMEI or select from the built-in preset lists. A modem reboot is required after applying.

---

## Default Configuration

| Setting     | Value         |
|-------------|---------------|
| Router IP   | 192.168.1.1   |
| Telnet Port | 2323          |
| Web UI Port | 5000          |
