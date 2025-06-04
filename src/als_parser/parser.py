import gzip
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Dict, Optional, Set
import os


class ALSParser:
    def __init__(self):
        self.supported_extensions = {'.als'}
    
    def decompress_als_file(self, als_path: Path) -> str:
        """Decompress an ALS file and return the XML content as string."""
        try:
            with gzip.open(als_path, 'rb') as f:
                return f.read().decode('utf-8')
        except Exception as e:
            raise ValueError(f"Failed to decompress {als_path}: {e}")
    
    def parse_xml_content(self, xml_content: str) -> ET.Element:
        """Parse XML content and return the root element."""
        try:
            return ET.fromstring(xml_content)
        except ET.ParseError as e:
            raise ValueError(f"Failed to parse XML content: {e}")
    
    def extract_file_references(self, root: ET.Element) -> Set[str]:
        """Extract all file references from the parsed XML."""
        file_refs = set()
        
        # Common patterns for file references in ALS files
        patterns = [
            './/FileRef/RelativePathElement',
            './/FileRef/Name',
            './/SampleRef/FileRef/RelativePathElement', 
            './/SampleRef/FileRef/Name',
            './/AudioClip/SampleRef/FileRef/RelativePathElement',
            './/AudioClip/SampleRef/FileRef/Name',
            './/Sample/ArrangerAutomation/Events/FloatEvent/ArrangerAutomation/Events/FloatEvent/ArrangerAutomation/Events/FloatEvent/ArrangerAutomation/Events/FloatEvent/Sample/ArrangerAutomation/Events/FloatEvent/Sample/ArrangerAutomation/Events/FloatEvent/Sample/FileRef/RelativePathElement',
            './/Sample/FileRef/RelativePathElement',
            './/Sample/FileRef/Name',
        ]
        
        for pattern in patterns:
            for element in root.findall(pattern):
                if element.text and element.text.strip():
                    # Clean up the path and extract just the filename
                    file_path = element.text.strip()
                    if file_path:
                        # Extract just the filename from the path
                        filename = os.path.basename(file_path)
                        if filename and not filename.startswith('.') and len(filename) > 1:
                            file_refs.add(filename)
        
        # Also look for direct Value elements that might contain file paths
        for value_elem in root.findall('.//Value'):
            if value_elem.text and value_elem.text.strip():
                text = value_elem.text.strip()
                # Check if it looks like a file path
                if any(ext in text.lower() for ext in ['.wav', '.aif', '.mp3', '.flac', '.m4a', '.ogg']):
                    filename = os.path.basename(text)
                    if filename and not filename.startswith('.') and len(filename) > 1:
                        file_refs.add(filename)
        
        # Check for file references in element attributes (especially Value attributes)
        for element in root.iter():
            for attr_name, attr_value in element.attrib.items():
                if attr_value and isinstance(attr_value, str):
                    # Check if the attribute value looks like a file path
                    if any(ext in attr_value.lower() for ext in ['.wav', '.aif', '.mp3', '.flac', '.m4a', '.ogg', '.aiff']):
                        filename = os.path.basename(attr_value)
                        if filename and not filename.startswith('.') and len(filename) > 1:
                            file_refs.add(filename)
        
        return file_refs
    
    def parse_als_file(self, als_path: Path) -> Dict[str, any]:
        """Parse a single ALS file and return metadata including file references."""
        if not als_path.exists():
            raise FileNotFoundError(f"ALS file not found: {als_path}")
        
        if als_path.suffix.lower() != '.als':
            raise ValueError(f"File is not an ALS file: {als_path}")
        
        # Decompress and parse
        xml_content = self.decompress_als_file(als_path)
        root = self.parse_xml_content(xml_content)
        
        # Extract file references
        file_refs = self.extract_file_references(root)
        
        return {
            'path': str(als_path),
            'filename': als_path.name,
            'file_references': sorted(list(file_refs)),
            'reference_count': len(file_refs)
        }
    
    def scan_directory(self, directory: Path, recursive: bool = True) -> List[Path]:
        """Scan directory for ALS files."""
        als_files = []
        
        if not directory.exists():
            raise FileNotFoundError(f"Directory not found: {directory}")
        
        if not directory.is_dir():
            raise ValueError(f"Path is not a directory: {directory}")
        
        pattern = "**/*.als" if recursive else "*.als"
        als_files = list(directory.glob(pattern))
        
        return sorted(als_files)
    
    def parse_multiple_files(self, als_files: List[Path]) -> List[Dict[str, any]]:
        """Parse multiple ALS files and return their metadata."""
        results = []
        
        for als_file in als_files:
            try:
                result = self.parse_als_file(als_file)
                results.append(result)
            except Exception as e:
                # Continue processing other files even if one fails
                results.append({
                    'path': str(als_file),
                    'filename': als_file.name,
                    'error': str(e),
                    'file_references': [],
                    'reference_count': 0
                })
        
        return results
    
    def search_for_sample(self, sample_name: str, directory: Path, recursive: bool = True) -> List[Dict[str, any]]:
        """Search for projects containing a specific sample file."""
        # Find all ALS files
        als_files = self.scan_directory(directory, recursive)
        
        # Parse all files
        all_results = self.parse_multiple_files(als_files)
        
        # Filter results that contain the sample
        matches = []
        for result in all_results:
            if 'error' not in result:
                # Check if sample name matches any file reference (case-insensitive)
                for file_ref in result['file_references']:
                    if sample_name.lower() in file_ref.lower():
                        matches.append({
                            **result,
                            'matched_reference': file_ref
                        })
                        break
        
        return matches