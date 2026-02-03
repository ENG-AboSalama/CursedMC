# 💀 CursedMC

<p align="center">
  <img src="TITLE.png" alt="CursedMC" width="600">
</p>

<p align="center">
  <strong>The Ultimate Linux Subsystem for Minecraft Server Management & CGNAT Bypass</strong>
</p>

<p align="center">
  <a href="#-installation"><img src="https://img.shields.io/badge/Platform-Linux%20%7C%20WSL2-blue" alt="Platform"></a>
  <a href="#-requirements"><img src="https://img.shields.io/badge/Java-17%2B-orange" alt="Java"></a>
  <a href="#-license"><img src="https://img.shields.io/badge/License-MIT-green" alt="License"></a>
</p>

---

CursedMC is a powerful, all-in-one lightweight solution designed to turn any Linux environment (VPS, WSL, etc.) into a fully managed Minecraft server host. It specializes in bypassing CGNAT restrictions, allowing you to host public servers even without a static IP or port forwarding capabilities.

## 🌟 Key Features

*   **🛡️ CGNAT Bypass**: Built-in integration with **Playit.gg** and **Ngrok** to expose your server and dashboard to the world instantly.
*   **💻 Web Dashboard**: Full control via a sleek, responsive web interface. No more terminal commands for daily tasks.
*   **⚡ Performance Optimized**: Includes automatic Aikar's Flags tuning for maximum TPS and stability.
*   **🧩 Mod & Plugin Manager**: Upload, enable, disable, and delete mods/plugins directly from the browser. Supports files up to **300MB**.
*   **📂 File Manager**: Full-featured web-based file browser and editor.
*   **🛠️ Multi-Version Support**: 
    *   **Paper** (High Performance)
    *   **Forge** (Modded)
    *   **Vanilla**
*   **📟 Live Console**: Real-time server console access from anywhere.
*   **📈 Resource Monitoring**: Live CPU, RAM, and Disk usage statistics.
*   **🔄 Auto-Recovery**: Automatic restart system in case of server crashes.

## 📸 Screenshots

<details>
<summary>Click to view screenshots</summary>

| Dashboard | Console | File Manager |
|-----------|---------|---------------|
| Coming Soon | Coming Soon | Coming Soon |

</details>

## 🚀 Installation

It takes just one command to set up everything (Java, Dependencies, Service, Dashboard).

**Run as root:**

```bash
sudo bash install.sh
```

Follow the on-screen instructions to complete the setup.

## 📖 Usage

### Accessing the Dashboard
Once installed, open your browser and navigate to:
`http://<YOUR-SERVER-IP>:8080`

### Default Ports
*   **Dashboard**: `8080`
*   **Minecraft**: `25565` (Tunneled via Playit.gg for public access)

### Management Commands
You can also manage the service from the terminal:

```bash
sudo bash install.sh status    # Check status
sudo bash install.sh restart   # Restart services
sudo bash install.sh update    # Update CursedMC
sudo bash install.sh uninstall # Remove completely
```

## ⚙️ Requirements

*   **OS**: Ubuntu/Debian (or WSL2)
*   **Java**: Java 21 (Recommended) or Java 17+ (Automatically installed)
*   **Python**: 3.8+

## 🔒 Security

*   First-time setup wizard enforces secure password creation.
*   Rate-limiting allows protection against brute-force attacks.
*   Dashboard access can be tunneled securely via Ngrok.

## 🤝 Contributing

Contributions are welcome! Feel free to:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

<p align="center">Made with 💜 by the CursedMC Team</p>
<p align="center">© 2026 CursedMC. All rights reserved.</p>
