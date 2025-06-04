import xml.etree.ElementTree as ET
import binascii
import re
from typing import List, Dict, Optional, Tuple
from pathlib import Path
import struct


class VSTPresetAnalyzer:
    """Analyzer for extracting VST preset information from ALS files."""
    
    def __init__(self):
        self.common_encodings = ['utf-8', 'utf-16', 'latin-1', 'ascii']
    
    def extract_vst_plugins(self, root: ET.Element) -> List[Dict]:
        """Extract all VST plugin information from ALS XML."""
        plugins = []
        
        for plugin_device in root.findall('.//PluginDevice'):
            vst_info = plugin_device.find('.//VstPluginInfo')
            if vst_info is not None:
                plugin_data = self._parse_vst_plugin_info(vst_info, plugin_device)
                if plugin_data:
                    plugins.append(plugin_data)
        
        return plugins
    
    def _parse_vst_plugin_info(self, vst_info: ET.Element, plugin_device: ET.Element) -> Optional[Dict]:
        """Parse VST plugin information and extract preset data."""
        # Basic plugin information
        plugin_data = {
            'plugin_name': self._get_element_value(vst_info, 'PlugName'),
            'file_name': self._get_element_value(vst_info, 'FileName'),
            'unique_id': self._get_element_value(vst_info, 'UniqueId'),
            'version': self._get_element_value(vst_info, 'Version'),
            'vst_version': self._get_element_value(vst_info, 'VstVersion'),
            'num_parameters': self._get_element_value(vst_info, 'NumberOfParameters'),
            'num_programs': self._get_element_value(vst_info, 'NumberOfPrograms'),
            'presets': []
        }
        
        # Extract preset data
        for vst_preset in vst_info.findall('.//VstPreset'):
            preset_data = self._analyze_vst_preset(vst_preset)
            if preset_data:
                plugin_data['presets'].append(preset_data)
        
        return plugin_data if plugin_data['presets'] else plugin_data
    
    def _analyze_vst_preset(self, vst_preset: ET.Element) -> Optional[Dict]:
        """Analyze a VST preset and extract readable information."""
        preset_data = {
            'type': self._get_element_value(vst_preset, 'Type'),
            'program_number': self._get_element_value(vst_preset, 'ProgramNumber'),
            'name': self._get_element_value(vst_preset, 'Name'),
            'parameter_count': self._get_element_value(vst_preset, 'ParameterCount'),
            'program_count': self._get_element_value(vst_preset, 'ProgramCount'),
            'readable_data': {},
            'raw_analysis': {}
        }
        
        # Get the binary buffer
        buffer_element = vst_preset.find('.//Buffer')
        if buffer_element is not None and buffer_element.text:
            buffer_data = buffer_element.text.strip()
            preset_data['buffer_size'] = len(buffer_data) // 2  # Hex chars to bytes
            
            # Analyze the buffer
            analysis_result = self._analyze_buffer(buffer_data)
            preset_data['readable_data'] = analysis_result['readable_data']
            preset_data['raw_analysis'] = analysis_result['raw_analysis']
        
        return preset_data
    
    def _analyze_buffer(self, hex_buffer: str) -> Dict:
        """Analyze VST preset buffer data using multiple strategies."""
        try:
            # Clean hex data by removing whitespace and newlines
            clean_hex = ''.join(hex_buffer.split())
            # Convert hex to bytes
            binary_data = binascii.unhexlify(clean_hex)
        except (binascii.Error, ValueError):
            return {'readable_data': {}, 'raw_analysis': {'error': 'Invalid hex data'}}
        
        analysis = {
            'readable_data': {},
            'raw_analysis': {
                'buffer_size': len(binary_data),
                'strategies_tried': []
            }
        }
        
        # Strategy 1: Look for embedded XML (most promising based on findings)
        xml_data = self._extract_embedded_xml(binary_data)
        if xml_data:
            analysis['readable_data']['embedded_xml'] = xml_data
            analysis['raw_analysis']['strategies_tried'].append('embedded_xml_success')
        else:
            analysis['raw_analysis']['strategies_tried'].append('embedded_xml_failed')
        
        # Strategy 2: Look for readable strings
        strings = self._extract_readable_strings(binary_data)
        if strings:
            analysis['readable_data']['strings'] = strings
            analysis['raw_analysis']['strategies_tried'].append('string_extraction_success')
        
        # Strategy 3: Look for common VST preset patterns
        patterns = self._analyze_common_patterns(binary_data)
        if patterns:
            analysis['readable_data']['patterns'] = patterns
            analysis['raw_analysis']['strategies_tried'].append('pattern_analysis_success')
        
        # Strategy 4: Try to decode as FXB/FXP format (common VST preset format)
        fxp_data = self._analyze_fxp_format(binary_data)
        if fxp_data:
            analysis['readable_data']['fxp_format'] = fxp_data
            analysis['raw_analysis']['strategies_tried'].append('fxp_format_success')
        
        return analysis
    
    def _extract_embedded_xml(self, binary_data: bytes) -> Optional[Dict]:
        """Extract embedded XML from binary data."""
        # Look for XML patterns
        xml_patterns = [
            b'<PresetChunkXMLTree',
            b'<Preset',
            b'<?xml',
            b'<root',
            b'<data'
        ]
        
        for pattern in xml_patterns:
            start_pos = binary_data.find(pattern)
            if start_pos != -1:
                # Try to find the end of the XML
                xml_candidates = []
                
                # Look for common XML endings
                end_patterns = [
                    b'</PresetChunkXMLTree>',
                    b'</Preset>',
                    b'</root>',
                    b'</data>'
                ]
                
                for end_pattern in end_patterns:
                    end_pos = binary_data.find(end_pattern, start_pos)
                    if end_pos != -1:
                        xml_data = binary_data[start_pos:end_pos + len(end_pattern)]
                        xml_candidates.append(xml_data)
                
                # If no clear end found, try extracting a reasonable chunk
                if not xml_candidates:
                    # Extract next 10KB or until null bytes
                    end_pos = min(start_pos + 10240, len(binary_data))
                    null_pos = binary_data.find(b'\x00', start_pos)
                    if null_pos != -1 and null_pos < end_pos:
                        end_pos = null_pos
                    xml_data = binary_data[start_pos:end_pos]
                    xml_candidates.append(xml_data)
                
                # Try to parse each candidate
                for xml_candidate in xml_candidates:
                    try:
                        # Try different encodings
                        for encoding in self.common_encodings:
                            try:
                                xml_text = xml_candidate.decode(encoding)
                                # Clean up the XML
                                xml_text = self._clean_xml_text(xml_text)
                                
                                # Parse the XML
                                root = ET.fromstring(xml_text)
                                return self._parse_preset_xml(root, xml_text)
                            except (UnicodeDecodeError, ET.ParseError):
                                continue
                    except Exception:
                        continue
        
        return None
    
    def _clean_xml_text(self, xml_text: str) -> str:
        """Clean XML text to make it parseable."""
        # Remove null bytes and other control characters
        xml_text = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', xml_text)
        
        # Ensure proper XML declaration if missing
        if not xml_text.strip().startswith('<?xml'):
            xml_text = '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_text
        
        return xml_text
    
    def _parse_preset_xml(self, root: ET.Element, xml_text: str) -> Dict:
        """Parse preset XML and extract meaningful information."""
        preset_info = {
            'xml_root_tag': root.tag,
            'raw_xml': xml_text[:1000] + '...' if len(xml_text) > 1000 else xml_text
        }
        
        # Look for preset name
        name_elements = root.findall('.//Preset[@Name]')
        if name_elements:
            name = name_elements[0].get('Name')
            if name:
                preset_info['preset_name'] = name
        
        # Look for plugin information
        plugin_name_elem = root.find('.//PluginName')
        if plugin_name_elem is not None and plugin_name_elem.text:
            preset_info['plugin_name'] = plugin_name_elem.text
        
        plugin_version_elem = root.find('.//PluginVersion')
        if plugin_version_elem is not None and plugin_version_elem.text:
            preset_info['plugin_version'] = plugin_version_elem.text
        
        # Look for parameters
        params_elem = root.find('.//Parameters')
        if params_elem is not None:
            params_type = params_elem.get('Type')
            if params_type:
                preset_info['parameters_type'] = params_type
            if params_elem.text:
                # Try to parse parameter values
                param_text = params_elem.text.strip()
                if param_text:
                    preset_info['parameter_values'] = param_text[:200] + '...' if len(param_text) > 200 else param_text
                    # Count parameters
                    param_values = param_text.split()
                    preset_info['parameter_count'] = str(len(param_values))
        
        # Look for setup information
        setup_elem = root.find('.//PresetData[@SetupName]')
        if setup_elem is not None:
            setup_name = setup_elem.get('SetupName')
            if setup_name:
                preset_info['setup_name'] = setup_name
        
        return preset_info
    
    def _extract_readable_strings(self, binary_data: bytes, min_length: int = 4) -> List[str]:
        """Extract readable ASCII strings from binary data."""
        strings = []
        
        for encoding in self.common_encodings:
            try:
                # Try to decode the entire buffer
                text = binary_data.decode(encoding, errors='ignore')
                # Extract printable strings
                import string
                printable_chars = string.printable
                current_string = ""
                
                for char in text:
                    if char in printable_chars and char not in '\t\n\r\x0b\x0c':
                        current_string += char
                    else:
                        if len(current_string) >= min_length:
                            strings.append(current_string)
                        current_string = ""
                
                if len(current_string) >= min_length:
                    strings.append(current_string)
                    
                if strings:
                    break
                    
            except Exception:
                continue
        
        # Remove duplicates and sort by length
        unique_strings = list(set(strings))
        unique_strings.sort(key=len, reverse=True)
        
        # Return top 20 longest strings
        return unique_strings[:20]
    
    def _analyze_common_patterns(self, binary_data: bytes) -> Dict:
        """Analyze common VST/plugin patterns in binary data."""
        patterns = {}
        
        # Look for common headers/magic numbers
        magic_numbers = {
            'CcnK': b'CcnK',  # VST chunk magic
            'FxCk': b'FxCk',  # VST effect chunk
            'FBCh': b'FBCh',  # VST bank chunk
            'VstP': b'VstP',  # VST preset
        }
        
        for name, magic in magic_numbers.items():
            if magic in binary_data:
                pos = binary_data.find(magic)
                patterns[f'{name}_found_at'] = pos
        
        # Look for float patterns (parameter values often stored as floats)
        float_values = []
        for i in range(0, len(binary_data) - 4, 4):
            try:
                value = struct.unpack('f', binary_data[i:i+4])[0]
                # Check if it's a reasonable parameter value (0.0 to 1.0 or similar)
                if 0.0 <= value <= 1.0 or -1.0 <= value <= 1.0:
                    float_values.append((i, value))
            except struct.error:
                continue
        
        if float_values:
            patterns['potential_float_params'] = float_values[:10]  # First 10
        
        return patterns
    
    def _analyze_fxp_format(self, binary_data: bytes) -> Optional[Dict]:
        """Try to parse data as FXP (VST preset) format."""
        if len(binary_data) < 28:  # Minimum FXP header size
            return None
        
        try:
            # FXP header structure
            magic = binary_data[0:4]
            if magic != b'CcnK':
                return None
            
            size = struct.unpack('>I', binary_data[4:8])[0]
            fxid = binary_data[8:12]
            version = struct.unpack('>I', binary_data[12:16])[0]
            plugin_id = struct.unpack('>I', binary_data[16:20])[0]
            plugin_version = struct.unpack('>I', binary_data[20:24])[0]
            num_params = struct.unpack('>I', binary_data[24:28])[0]
            
            fxp_data = {
                'format': 'FXP',
                'magic': magic.decode('ascii', errors='ignore'),
                'size': size,
                'fxid': fxid.decode('ascii', errors='ignore'),
                'version': version,
                'plugin_id': plugin_id,
                'plugin_version': plugin_version,
                'num_parameters': num_params
            }
            
            # Try to extract preset name (usually at offset 28, 28 chars)
            if len(binary_data) >= 56:
                preset_name = binary_data[28:56].decode('ascii', errors='ignore').rstrip('\x00')
                if preset_name:
                    fxp_data['preset_name'] = preset_name
            
            return fxp_data
            
        except (struct.error, UnicodeDecodeError):
            return None
    
    def _get_element_value(self, parent: ET.Element, tag_name: str) -> Optional[str]:
        """Get the Value attribute of a child element."""
        element = parent.find(f'.//{tag_name}')
        if element is not None:
            return element.get('Value')
        return None