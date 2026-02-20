# ⚡ Nora Energy Management System

<div align="center">
  <img src="Nora-LOGO/Nora-LoGo.jpeg" alt="Nora Energy Logo" width="200"/>
  
  ### Professional Energy Dashboard for Enterprise Buildings
  
  [![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://www.python.org/)
  [![Flask](https://img.shields.io/badge/Flask-2.0+-green.svg)](https://flask.palletsprojects.com/)
  [![License](https://img.shields.io/badge/License-Proprietary-red.svg)](LICENSE)
  
  **Real-time Energy Monitoring • Smart Tagging • Cost Analytics**
</div>

---

## 📋 Overview

Nora Energy Management System is a sophisticated web-based dashboard designed for enterprise buildings to monitor, analyze, and optimize energy consumption across multiple sources. The system provides real-time insights into utility grid power, generator backup, and tenant-specific consumption patterns.

### ✨ Key Features

- **🔌 Multi-Source Tracking** - Monitor utility grid and generator power consumption simultaneously
- **🏢 Tenant Analytics** - Smart tagging algorithm automatically splits consumption by power source
- **💰 Cost Management** - Real-time cost calculations and credit balance tracking
- **📊 Visual Analytics** - Interactive charts with 10-day trend analysis
- **⚡ Real-time Data** - Live updates from SQLite database with meter readings
- **📱 Responsive Design** - Professional UI optimized for desktop and mobile devices

---

## 🚀 Quick Start

### Prerequisites

- Python 3.8 or higher
- pip package manager
- Modern web browser

### Installation

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd nora_energy_dashboard_mock_tagging
   ```

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Run the application**
   ```bash
   python3 webapp.py
   ```

4. **Access the dashboard**
   ```
   Open your browser and navigate to: http://localhost:5000
   ```

---

## 🏗️ Architecture

### Technology Stack

| Component | Technology |
|-----------|-----------|
| **Backend** | Python 3, Flask |
| **Database** | SQLite 3 |
| **Frontend** | HTML5, CSS3, JavaScript |
| **Charts** | Chart.js |
| **Styling** | Custom CSS with Inter Font |

### Project Structure

```
nora_energy_dashboard_mock_tagging/
├── webapp.py                 # Main Flask application
├── ems.db                    # SQLite database
├── requirements.txt          # Python dependencies
├── static/
│   ├── styles.css           # Professional UI styling
│   └── Nora-LoGo.jpeg       # Company logo
├── templates/
│   └── dashboard.html       # Main dashboard template
└── Nora-LOGO/
    └── Nora-LoGo.jpeg       # Original logo file
```

---

## 💡 How It Works

### Smart Tagging Algorithm

The system uses an intelligent tagging algorithm to determine the power source for tenant consumption:

1. **Generator Detection** - If generator meter increases → Tag as GENERATOR source
2. **Utility Detection** - If utility meter increases → Tag as UTILITY source
3. **Persistence** - Maintains last known source during idle periods

### Data Flow

```
Meter Readings → SQLite Database → Flask Backend → Dashboard UI
                                         ↓
                                  Analytics Engine
                                         ↓
                              Cost Calculations & Forecasting
```

---

## 📊 Dashboard Features

### 1. Time Range Configuration
- Flexible date and time filtering
- Custom range selection for detailed analysis
- Real-time data refresh

### 2. Power Source Monitoring
- **Utility Grid** - Track grid power consumption and costs
- **Generator Backup** - Monitor backup power usage
- **Tenant Consumption** - Detailed breakdown by power source

### 3. Credit Management
- Top-up tracking with timestamps
- Automatic cost calculations
- Balance forecasting based on historical data
- Days remaining prediction

### 4. Visual Analytics
- 10-day consumption trends
- Multi-source comparison charts
- Interactive data visualization
- Color-coded power sources

---

## ⚙️ Configuration

### Environment Variables

```bash
# Database path
export EMS_DB="ems.db"

# Tariff rate (RWF per kWh)
export TARIFF_RWF_PER_KWH="255"

# Forecast window (days)
export FORECAST_WINDOW_DAYS="7"

# Tagging thresholds (kWh)
export GEN_THRESHOLD_KWH="0.0001"
export UTIL_THRESHOLD_KWH="0.0001"
```

### Meter Configuration

| Meter ID | Type | Description |
|----------|------|-------------|
| 10 | Utility | Main grid power meter |
| 21 | Generator | Backup generator meter |
| 13 | Tenant | Supermarket consumption |

---

## 📈 Use Cases

### For Building Managers
- Monitor total energy consumption across all sources
- Track utility costs and manage budgets
- Forecast credit depletion and plan top-ups

### For Tenants
- View detailed consumption breakdowns
- Understand power source distribution
- Analyze usage patterns over time

### For Finance Teams
- Accurate cost allocation by power source
- Historical spending analysis
- Budget planning and forecasting

---

## 🎨 UI/UX Design

### Design Principles
- **Clean & Professional** - Corporate-grade interface suitable for executive presentations
- **High Contrast** - Easy-to-read data visualization for quick insights
- **Responsive Layout** - Optimized for all screen sizes
- **Intuitive Navigation** - Minimal learning curve for non-technical users

### Color Scheme
- **Primary**: Professional blue (#0ea5e9) - Trust and reliability
- **Utility**: Amber (#f59e0b) - Traditional power source
- **Generator**: Green (#10b981) - Backup/alternative energy
- **Tenant**: Purple (#8b5cf6) - Distinct identification

---

## 🔒 Security & Data

- Local SQLite database for data privacy
- No external API dependencies for core functionality
- Timezone-aware timestamps (Africa/Kigali)
- Input validation on all forms

---

## 📝 Database Schema

### Tables

**readings** - Meter readings with timestamps
- `meter_id` - Meter identifier
- `ts` - Timestamp (local time)
- `epp_kwh` - Energy reading in kWh
- `role` - Meter role (utility/generator/tenant)

**topups** - Credit top-up records
- `ts` - Timestamp
- `amount_rwf` - Amount in Rwandan Francs
- `note` - Optional description

---

## 🛠️ Development

### Running in Debug Mode

```bash
# Enable Flask debug mode
export FLASK_DEBUG=1
python3 webapp.py
```

### Adding New Tenants

Edit `webapp.py` and update the `TENANTS` list:

```python
TENANTS = [
    {"id": 13, "name": "Supermarket"},
    {"id": 14, "name": "Restaurant"},  # Add new tenant
]
```

---

## 📞 Support

For technical support or inquiries, please contact:

**Nora Energy Management System**  
Enterprise Solutions Division  
© 2026 All Rights Reserved

---

## 🏆 Credits

Developed with ❤️ for enterprise energy management

**Powered by:**
- Flask Web Framework
- Chart.js Visualization Library
- Inter Font Family
- Modern Web Standards

---

<div align="center">
  <img src="Nora-LOGO/Nora-LoGo.jpeg" alt="Nora Energy" width="100"/>
  
  ### Nora Energy Management System
  **Intelligent Energy • Sustainable Future**
  
  ---
  
  Made with ⚡ by Nora Energy Team
</div>
