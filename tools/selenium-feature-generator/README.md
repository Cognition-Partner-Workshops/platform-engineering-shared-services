# Selenium Feature-File Generator

A reusable Java CLI tool that generates **Cucumber/BDD `.feature` files** for Java Selenium tests from plain-text **user stories**, powered by the **Infosys AI Gateway**.

## Features

- Accepts user stories via **file**, **inline argument**, or **stdin**
- Generates well-structured Gherkin `.feature` files with:
  - Happy-path scenarios
  - Edge-case and negative scenarios
  - Scenario Outlines with Examples tables
  - Selenium-oriented step definitions (browser interactions)
  - Meaningful tags (`@smoke`, `@regression`, `@positive`, `@negative`)
- Automatically derives file names from the `Feature:` title
- Outputs files to a configurable directory

## Prerequisites

- **Java 17+**
- **Maven 3.6+**
- **INFOSYS_CODER_API_KEY** environment variable set with your Infosys AI Gateway API key

## Build

```bash
mvn clean package -q
```

This produces an executable fat JAR at `target/feature-generator-1.0.0.jar`.

## Usage

```bash
# Set your API key
export INFOSYS_CODER_API_KEY="your-api-key-here"

# Option 1: Read user story from a file
java -jar target/feature-generator-1.0.0.jar --file story.txt

# Option 2: Pass user story inline
java -jar target/feature-generator-1.0.0.jar --story "As a registered user, I want to log in to the application so that I can access my dashboard."

# Option 3: Pipe from stdin
cat story.txt | java -jar target/feature-generator-1.0.0.jar

# Specify custom output directory (default: ./output)
java -jar target/feature-generator-1.0.0.jar --file story.txt --output ./features
```

### CLI Options

| Option | Short | Description |
|---|---|---|
| `--file <path>` | `-f` | Read user story from a text file |
| `--story <text>` | `-s` | Pass user story as an inline string |
| `--output <dir>` | `-o` | Output directory (default: `./output`) |
| `--help` | `-h` | Show help message |

## Example

**Input** (`story.txt`):

```
As a registered user, I want to log in to the web application
so that I can access my personalised dashboard.

Acceptance Criteria:
- User can enter username and password on the login page
- Valid credentials redirect to the dashboard
- Invalid credentials show an error message
- Account locks after 3 failed attempts
- "Forgot password" link is available on the login page
```

**Run**:

```bash
java -jar target/feature-generator-1.0.0.jar --file story.txt --output ./features
```

**Output** (`./features/user_login.feature`):

A complete `.feature` file with multiple scenarios covering the happy path, invalid credentials, account lockout, forgot-password link, and more.

## Project Structure

```
selenium-feature-generator/
├── pom.xml
├── README.md
├── src/main/java/com/infosys/selenium/generator/
│   ├── Main.java                  # CLI entry point
│   ├── AIGatewayClient.java       # HTTP client for AI Gateway
│   └── FeatureFileGenerator.java  # Prompt + generation logic
└── output/                        # Default output directory
```

## Customisation

### Changing the AI Model

Edit `AIGatewayClient.java` and update the `"model"` field in `buildRequestBody()`.

### Adjusting the Prompt

Edit the `SYSTEM_PROMPT` constant in `FeatureFileGenerator.java` to tailor the generated output (e.g., add project-specific conventions, step-definition patterns, or additional tags).
