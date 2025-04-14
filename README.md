# 🧠 Amiga-Actuator Control with CAN & GPS Integration 🚜

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
