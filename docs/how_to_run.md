# How to Run

This guide covers running all components: database setup, backend API, web UI, and LLM server.

---

## Prerequisites

```bash
pip install fastapi uvicorn pydantic langchain langchain-community langchain-openai sqlalchemy pytz
```

For real LLM inference (requires GPU):
```bash
pip install vllm
```

---

## 1. Database Setup

Seed the database with truck and site registries:

```bash
python3 -m fleet_pipeline.db.seed_data
```

This creates `fleet_pipeline/data/fleet.db` and populates the trucks and sites tables.

---

## 2. Backend API + Web UI

Start the FastAPI server (includes the web UI):

```bash
python3 -m fleet_pipeline.api.main
```

Or equivalently:

```bash
uvicorn fleet_pipeline.api.main:app --reload --port 8000
```

- **Web UI**: http://localhost:8000
- **API Docs**: http://localhost:8000/docs

---

## 3. LLM Server (Optional)

For real LLM inference instead of mock mode, start vLLM:

```bash
vllm serve Qwen/Qwen2.5-7B-Instruct-AWQ --port 8001 --tensor-parallel-size 1
```

This runs an OpenAI-compatible API at `http://localhost:8001/v1`.

---

## 4. Running Simulations

### Mock Mode (No GPU)

```bash
python3 -m fleet_pipeline.simulation.run_simulation \
    --input raw_data_level1_2/chat.txt \
    --mock
```

### Real LLM Mode

```bash
python3 -m fleet_pipeline.simulation.run_simulation \
    --input raw_data_level1_2/chat.txt
```

### Other Options

```bash
# Single shift only
python3 -m fleet_pipeline.simulation.run_simulation \
    --input raw_data_level1_2/chat.txt \
    --mock \
    --shift 20231015_shift_1

# Verbose output
python3 -m fleet_pipeline.simulation.run_simulation \
    --input raw_data_level1_2/chat.txt \
    --mock --verbose
```

---

## Quick Start (All-in-One)

1. **Seed database**:
   ```bash
   python3 -m fleet_pipeline.db.seed_data
   ```

2. **Start API + Web UI**:
   ```bash
   python3 -m fleet_pipeline.api.main
   ```

3. **Open browser**: http://localhost:8000

4. **Run a simulation** (in another terminal):
   ```bash
   python3 -m fleet_pipeline.simulation.run_simulation \
       --input raw_data_level1_2/chat.txt \
       --mock
   ```

5. **Refresh the web UI** to see the results.

---

## Service Ports

| Service | URL |
|---------|-----|
| Web UI + API | http://localhost:8000 |
| API Docs | http://localhost:8000/docs |
| vLLM (if running) | http://localhost:8001/v1 |

---

## Project Structure

```
fleet_pipeline/
├── api/                    # FastAPI backend
│   ├── main.py            # Entry point
│   └── routes/            # API endpoints
├── frontend/              # Web UI (HTML/JS)
├── pipeline/              # Level 1, 2, 3 processing
├── simulation/            # Run simulations
├── db/                    # Database & seeding
├── data/                  # SQLite database (fleet.db)
└── prompts/               # LLM prompt templates
```
