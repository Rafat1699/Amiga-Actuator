# 🧠 Amiga-Actuator Control with CAN & GPS Integration (developed by Rafat)🚜

This repository contains the control setup and Python code for actuator control on the Amiga Brain using GPS data and CAN bus integration.  
Follow this step-by-step guide to run your actuator smoothly!

## 🗂️ Table of Contents

- [Overview](#overview)
- [Prerequisites](#prerequisites)
- [Setup Guide](#setup-guide)
- [Create and Place the Python Code](#create-and-place-the-python-code)
- [Running the Application](#running-the-application)
- [Notes](#notes)
- [License](#license)
- [Acknowledgments](#acknowledgments)

---

## 🌟 Overview

This project allows you to control an actuator connected to the Amiga Brain, utilizing CAN bus communication and GPS data.  
The actuator commands are managed via `python-can` and GPS input is used for logic control.

---

## 🔧 Prerequisites

- Access to the **Amiga Brain** via SSH.
- Virtual environment already created on the brain.
- CAN bus and GPS properly set up and connected.

---

## 🚀 Setup Guide

### Step 1: SSH into the Amiga Brain

Open your terminal and SSH into the Amiga Brain as per your usual procedure.

### Step 2: Activate Virtual Environment

If your virtual environment is already created, activate it:

```bash
source venv/bin/activate

### Step 1: SSH into the Amiga Brain

```bash
pip install python-can

### Step 4: Start CAN Generator

```bash
cangen can0 -I 701 -L 1 -D 05 -g 100

⚠️ Important:

Keep this terminal open and running at all times!
Do not close this terminal.

### Step 5: Open New Terminal for Next Steps

Open a new terminal window.

SSH back into the Amiga Brain.

You can now use VSCode to paste your Python code into the brain’s filesystem.

### Step 6: Navigate tot he GPS Client Directory

```bash
cd farm-ng-amiga/py/examples/gps_clients

create a python script in this directory name your code file and the paste the code from actuator.py that is in the repo

### Step 7: Run the application

```bash
python3 actuator.py --service-config service_config.json

🗒️ Notes
Always keep the first terminal open running the cangen command.

Make sure the Python virtual environment is activated before running your script.

Keep your service_config.json in the same directory as your Python script.

Use VSCode or any editor of your choice to manage the code directly on the brain.

📜 License
This project is licensed under the MIT License.

🙌 Acknowledgments
Amiga Farm-ng Team for their amazing work on open agricultural robotics.

python-can library contributors.

VSCode Remote SSH for seamless code editing inside the Amiga Brain.
