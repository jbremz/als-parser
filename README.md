# ALS Parser

A Python tool to parse Ableton Live Set (.als) files and search for sample references. Perfect for reverse sample searching - find which projects use specific samples across your entire music library.

## Features

- 🔍 **Reverse Sample Search**: Find all projects containing a specific sample file
- 📂 **Directory Scanning**: Recursively scan directories for ALS files
- 📊 **Usage Statistics**: Analyze sample usage patterns across projects
- 💾 **Export Results**: Save results to JSON for further analysis
- 🚀 **Fast & Reliable**: Built with Python's standard library for maximum compatibility

## Installation

This project uses [uv](https://docs.astral.sh/uv/) for Python package management. If you don't have uv installed:

```bash
# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then install the project:

```bash
# Clone the repository
git clone <repository-url>
cd als-parser

# Install dependencies and the package
uv sync
uv pip install -e .
```

## Usage

### Command Line Interface

The tool provides three main commands:

#### 1. Scan for ALS files and extract sample references

```bash
# Scan a single ALS file
als-parser scan /path/to/project.als

# Scan a directory recursively
als-parser scan /path/to/projects --recursive

# Scan with verbose output
als-parser scan /path/to/projects --verbose

# Save results to JSON file
als-parser scan /path/to/projects --output results.json
```

#### 2. Search for projects containing a specific sample

```bash
# Search for a sample (case-insensitive by default)
als-parser search "kick_drum.wav" /path/to/projects

# Exact filename match (case-sensitive)
als-parser search "Kick_Drum.wav" /path/to/projects --exact

# Save search results to file
als-parser search "sample.wav" /path/to/projects --output matches.json
```

#### 3. Get statistics about sample usage

```bash
# Show top 10 most used samples
als-parser stats /path/to/projects

# Show top 20 most used samples
als-parser stats /path/to/projects --top 20
```

### Python API

You can also use the parser directly in your Python code:

```python
from als_parser import ALSParser
from pathlib import Path

# Initialize the parser
parser = ALSParser()

# Parse a single ALS file
result = parser.parse_als_file(Path("project.als"))
print(f"Found {result['reference_count']} sample references")
print(result['file_references'])

# Search for a specific sample
matches = parser.search_for_sample("kick.wav", Path("/music/projects"))
for match in matches:
    print(f"Found in: {match['filename']}")
    print(f"Matched: {match['matched_reference']}")

# Scan directory for all ALS files
als_files = parser.scan_directory(Path("/music/projects"), recursive=True)
results = parser.parse_multiple_files(als_files)
```

## How It Works

ALS files are compressed XML files using gzip compression. The parser:

1. **Decompresses** the .als file using Python's gzip module
2. **Parses** the resulting XML using ElementTree
3. **Extracts** file references from various XML elements that contain sample paths
4. **Filters** and cleans the extracted paths to get just filenames
5. **Returns** structured data about the samples used in each project

### Supported File References

The parser looks for sample references in various XML elements commonly used by Ableton Live:

- Direct file references (`FileRef/RelativePathElement`, `FileRef/Name`)
- Sample references (`SampleRef/FileRef/*`)
- Audio clip references (`AudioClip/SampleRef/FileRef/*`)
- Value elements containing file paths

## Example Output

### Scanning Projects

```bash
$ als-parser scan ~/Music/Ableton --verbose

Scanning directory: ~/Music/Ableton
Recursive scan enabled
Found 15 ALS files

📁 Track 1.als
   Path: ~/Music/Ableton/Track 1.als
   References: 8
   • kick_909.wav
   • bass_synth.wav
   • vocal_chop.aif
   • crash_cymbal.wav

📁 Track 2.als
   Path: ~/Music/Ableton/Track 2.als
   References: 5
   • kick_909.wav
   • snare_acoustic.wav
   • pad_ambient.wav

✅ Processed 15 files, found 67 total sample references
```

### Searching for Samples

```bash
$ als-parser search "kick_909.wav" ~/Music/Ableton

🔍 Searching for sample: 'kick_909.wav'
📂 Search path: ~/Music/Ableton

✅ Found 3 project(s) containing 'kick_909.wav':

📁 Track 1.als
   Path: ~/Music/Ableton/Track 1.als
   Matched: kick_909.wav
   Total references: 8

📁 Track 2.als
   Path: ~/Music/Ableton/Track 2.als
   Matched: kick_909.wav
   Total references: 5

📁 Remix Project.als
   Path: ~/Music/Ableton/Remix Project.als
   Matched: kick_909.wav
   Total references: 12
```

### Usage Statistics

```bash
$ als-parser stats ~/Music/Ableton

📊 Analyzing sample usage in: ~/Music/Ableton

📈 Statistics:
   Total projects: 15
   Total sample references: 67
   Unique samples: 34

🔥 Top 10 most used samples:
    1. kick_909.wav (5 projects)
    2. snare_acoustic.wav (4 projects)
    3. bass_synth.wav (3 projects)
    4. vocal_chop.aif (3 projects)
    5. crash_cymbal.wav (2 projects)
    ...
```

## Development

### Setting up Development Environment

```bash
# Install development dependencies
uv sync --dev

# Run tests
uv run pytest

# Install in development mode
uv pip install -e .
```

### Running Tests

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=als_parser
```

## Troubleshooting

### Common Issues

1. **"Failed to decompress ALS file"**
   - Ensure the file is a valid .als file
   - Check file permissions
   - File might be corrupted

2. **"No ALS files found"**
   - Check the directory path is correct
   - Ensure you have read permissions for the directory
   - Try with `--recursive` flag if files are in subdirectories

3. **"Failed to parse XML content"**
   - The ALS file might be from a very old or very new version of Ableton Live
   - File might be corrupted during decompression

### Getting Help

If you encounter issues:

1. Check that your ALS files can be opened in Ableton Live
2. Try parsing a single file first before scanning directories
3. Use the `--verbose` flag to see detailed output
4. Check file permissions on the directories you're scanning

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add some amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request