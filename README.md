# Prefeitura Rio MCP Server

A Model Context Protocol (MCP) server implementation for Prefeitura do Rio de Janeiro with a modular, production-ready architecture.

## About MCP

The [Model Context Protocol (MCP)](https://modelcontextprotocol.io) allows building servers that expose data and functionality to LLM applications in a secure, standardized way.

## Features

- Basic calculator functions (add, subtract, multiply, divide)
- Rio de Janeiro information (districts list)
- Date and time functions for Rio timezone
- Personalized greeting resource
- Modular architecture for maintainability and scalability
- Testing suite and development tools integration
- Command-line interface for configurable server deployment

## Project Structure

```
app-mcp-server/
├── app_mcp_server/        # Main package
│   ├── __init__.py       # Package initialization
│   ├── __main__.py       # Entry point for package execution
│   ├── app.py            # Application factory and configuration
│   ├── config/           # Configuration settings
│   ├── resources/        # MCP resources
│   └── tools/            # MCP tools
├── tests/                # Test suite
├── cli.py                # CLI for running the server
├── pyproject.toml        # Project metadata and dependencies
├── README.md            # Project documentation
└── uv.lock               # Dependency lock file
```

## Installation

This project uses [uv](https://docs.astral.sh/uv/) for dependency management.

```bash
# Clone the repository
git clone <repository-url>
cd app-mcp-server

# Create and activate virtual environment
uv venv
source .venv/bin/activate

# Install dependencies
uv sync

# Install in development mode
uv pip install -e ".[dev]"
```

## Running the Server

### Using the CLI

The most straightforward way to run the server:

```bash
# Run with default settings
python cli.py

# Run with custom host and port
python cli.py --host 127.0.0.1 --port 8080 --debug
```

### As a Python Module

```bash
# Run directly as a module
python -m app_mcp_server
```

### Using MCP Development Tools

```bash
# Use MCP's development server
uv run mcp dev app_mcp_server/app.py
```

### Using the Entry Point

After installing the package:

```bash
# Use the installed entry point
mcp-server
```

## Development

### Running Tests

```bash
# Run tests with pytest
python -m pytest

# With coverage report
python -m pytest --cov=app_mcp_server
```

### Code Quality Tools

```bash
# Format code with black
python -m black .

# Sort imports with isort
python -m isort .

# Lint with ruff
python -m ruff check .

# Type checking with mypy
python -m mypy app_mcp_server
```

## Extending the Server

### Adding a New Tool

Create a new file in the `app_mcp_server/tools/` directory:

```python
# app_mcp_server/tools/new_tools.py
def my_new_tool(param1: type, param2: type) -> return_type:
    """Description of what the tool does"""
    # Your tool implementation
    return result
```

Then register it in `app_mcp_server/app.py`:

```python
from app_mcp_server.tools.new_tools import my_new_tool

def create_app() -> FastMCP:
    # ...existing code...
    mcp.tool()(my_new_tool)
    # ...existing code...
```

### Adding a New Resource

Create a new file in the `app_mcp_server/resources/` directory:

```python
# app_mcp_server/resources/new_resource.py
def get_new_resource() -> return_type:
    """Description of what the resource provides"""
    # Your resource implementation
    return data
```

Then register it in `app_mcp_server/app.py`:

```python
from app_mcp_server.resources.new_resource import get_new_resource
from app_mcp_server.config.settings import RESOURCE_PREFIX

def create_app() -> FastMCP:
    # ...existing code...
    mcp.resource(f"{RESOURCE_PREFIX}new_resource")(get_new_resource)
    # ...existing code...
```

## License

MIT License - See the LICENSE file for details.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add some amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request