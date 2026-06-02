"""
Shell command generation using Fireworks AI.

This module provides a CLI command to generate shell commands from natural
language descriptions using Fireworks AI with glm-5 model.
"""

import os
import sys
from typing import Optional

import click
import pyperclip
from dotenv import load_dotenv

from cli_tools.inference.fireworks import get_client as get_fireworks_client

# Load environment variables
load_dotenv()


async def generate_shell_command(description: str) -> str:
    """
    Generate a shell command from natural language description using Fireworks AI.
    
    Args:
        description: Natural language description of desired command
    
    Returns:
        Generated shell command
    
    Raises:
        Exception: If API call fails
    """
    # System instructions for command generation
    system_instruction = """You are a shell command generator. Generate ONLY the command, nothing else.

Rules:
1. Output ONLY the executable command
2. No explanations, no markdown, no backticks
3. Generate PowerShell syntax by default unless the user explicitly specifies another shell (bash, cmd, etc.)
4. Make commands safe and non-destructive when possible
5. Use common best practices
6. If the request is NOT about generating a shell command, respond with EXACTLY: OUT_OF_SCOPE

Examples:
Input: "list all python files in current directory"
Output: Get-ChildItem -Filter *.py

Input: "find all files larger than 100MB"
Output: Get-ChildItem -Recurse | Where-Object {$_.Length -gt 100MB}

Input: "kill process on port 8000"
Output: Get-Process -Id (Get-NetTCPConnection -LocalPort 8000).OwningProcess | Stop-Process

Input: "list files in bash"
Output: ls -la

Input: "tell me a joke"
Output: OUT_OF_SCOPE"""
    
    # Prepare user message
    user_message = f"Generate a shell command for: {description}"
    
    try:
        client = get_fireworks_client()
        
        result = await client.complete(
            system_prompt=system_instruction,
            user_prompt=user_message,
            model="accounts/fireworks/models/glm-5p1",
            temperature=0.3,
            max_tokens=200,
            reasoning_effort="none",
        )
        
        if not result:
            raise RuntimeError("No response from Fireworks API")
        
        # Handle both (content, usage) and (content, reasoning, usage) returns
        if len(result) == 3:
            command, _reasoning, usage = result
        else:
            command, usage = result
        
        if not command.strip():
            raise RuntimeError("Empty response from Fireworks API")
        
        # Clean up the command (remove any markdown formatting if present)
        command = command.strip()
        if command.startswith('```'):
            # Remove code block markers
            lines = command.split('\n')
            command = '\n'.join(lines[1:-1]) if len(lines) > 2 else command
            command = command.strip()
        
        # Check if request is out of scope
        if command == "OUT_OF_SCOPE":
            raise ValueError("Request is not related to command generation")
        
        return command
        
    except ValueError:
        # Out of scope request - re-raise as-is
        raise
    except Exception as e:
        raise RuntimeError(f"Failed to generate command: {e}") from e


@click.command()
@click.argument('description', nargs=-1, required=True)
def command_generator(description: tuple) -> None:
    """
    Generate shell command from natural language description.
    
    Examples:
        tool command list all python files
        tool command find files larger than 100MB
        tool command kill process on port 8000
    """
    import asyncio
    
    # Join description words into a single string
    description_str = ' '.join(description)
    
    if not description_str.strip():
        click.echo("❌ Please provide a command description", err=True)
        sys.exit(1)
    
    try:
        # Generate command
        command = asyncio.run(generate_shell_command(description_str))
        
        # Display result
        click.echo(command)
        
        # Copy to clipboard (skips silently on headless systems)
        try:
            pyperclip.copy(command)
            click.echo("\n✓ Command copied to clipboard")
        except pyperclip.PyperclipException:
            pass
        
    except ValueError as e:
        # Out of scope or invalid request
        if "not related to command generation" in str(e):
            click.echo("⚠️  This request is not about generating shell commands", err=True)
            sys.exit(0)  # Exit gracefully
        else:
            click.echo(f"❌ {e}", err=True)
            sys.exit(1)
    except Exception as e:
        click.echo(f"❌ {e}", err=True)
        sys.exit(1)


if __name__ == '__main__':
    command_generator()
