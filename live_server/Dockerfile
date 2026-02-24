# syntax=docker/dockerfile:1

# 1. Build Stage: Install dependencies into a virtual environment
FROM python:3.11-slim AS build

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Create a virtual environment
RUN uv venv

# Copy dependency definitions
COPY pyproject.toml uv.lock* ./
# uv.lock* is used to copy uv.lock if it exists

# Install dependencies into the venv
# Use cache mounting to speed up subsequent builds
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync

# 2. Final Stage: Create the production image
FROM python:3.11-slim

WORKDIR /app

# Copy the virtual environment with all dependencies from the build stage
COPY --from=build /app/.venv ./.venv

# Make port 5000 available to the world outside this container
EXPOSE 5000