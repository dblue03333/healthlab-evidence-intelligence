# HealthLab Evidence Intelligence

An evidence-grounded intelligence system that searches medical literature (PubMed), extracts structured findings, and synthesizes traceable Evidence Briefs for the HealthLab team.

> **Current Milestone:** v0.1.0 Pilot (Target: 13/09/2026)  
> **Core Guarantee:** Every factual claim in an Evidence Brief must map directly to:  
> `Claim → EvidenceItem → Supporting Passage → DocumentSnapshot → PubMed Record`

---

## Project Status & Scope

* **Pilot Scope:** See [docs/pilot-scope.md](docs/pilot-scope.md) for supported inputs, output brief format, and boundaries.
* **Literature Source (v0.1.0):** PubMed metadata and abstracts only via NCBI E-Utilities.
* **Extraction Model:** FPT AI Model (structured JSON extraction with passage verification).

---

## Quickstart

### 1. Prerequisites
* Python 3.11+
* Git

### 2. Environment Setup
```bash
# Clone the repository
git clone https://github.com/dblue03333/healthlab-evidence-intelligence.git
cd healthlab-evidence-intelligence

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies (development mode)
pip install -e ".[dev]"
```

### 3. Configuration
Copy the environment template and add your credentials:
```bash
cp .env.example .env
```

### 4. Running Tests
```bash
pytest
```

---

## Project Structure

```text
├── docs/             # Project specifications and contracts (pilot-scope.md)
├── src/healthlab/    # Core Python application logic
├── tests/            # Automated test suite (unit, integration, fixtures)
├── data/             # Sample questions and fixtures
├── outputs/          # Output templates and run artifacts
├── pyproject.toml    # Dependencies and build configuration
└── .env.example      # Environment variables template (no secrets)
```
