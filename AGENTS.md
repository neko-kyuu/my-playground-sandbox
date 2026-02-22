# Guidelines

This file provides guidance when working with code in this repository.

## Project Overview

This is a **playground** for testing new features, experimental purposes, and Minimum Viable Product. This repository typically does not require unit testing or complex design; it focuses on rapid implementation.

Each directory is distinguished by its purpose, and the naming follows the principles of clarity and distinctiveness.
If you want to add a major feature (NOT a refactoring) to existing code, it's best to create a new folder rather than modifying the original code directly. This is to allow for better comparative experiments.

## Guidelines for Creating New Directories

When user requests your assistance in developing a new feature, first check if there is a field (top-level directory) and a facet (subdirectory).

For example, if user requests, "Help me implement a certain function in the generation phase of RAG," check if a top-level directory containing `RAG` or a similar concept exists. Then, check if a subdirectory containing `generation` or a similar concept exists.

Sometimes user may specify their own paths.

## Guidelines for README 

When working in a specific directory, it's helpful to first read the README.md file to understand the process.
If you are creating a new directory, be sure to create a README. Describe the purpose of each directory in short, but environment setup should be documented in detail.

The version installations/dependency installations (such as `uv pip install`, `uv add`, `npm install`, etc.) mentioned in the documentation have usually already been executed; they are only for record-keeping and migration convenience.

If you install new dependency packages, be sure to record it in the README.md file.

## Requirement

Use `uv` to manage Python versions, create virtual environments, and install dependencies. The virtual environment needs to be created in the same directory as README.

## Prohibit

[IMPORTANT]: Do not execute scripts other than dependency installations without the user's explicit permission, as this may involve API fees.