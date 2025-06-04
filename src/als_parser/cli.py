import click
from pathlib import Path
from .parser import ALSParser
from .vst_analyzer import VSTPresetAnalyzer
import json


@click.group()
@click.version_option(version="0.1.0")
def main():
    """ALS Parser - A tool to parse Ableton Live Set (.als) files and search for sample references."""
    pass


@main.command()
@click.argument('path', type=click.Path(exists=True, path_type=Path))
@click.option('--recursive/--no-recursive', default=True, help='Scan directories recursively')
@click.option('--output', '-o', type=click.Path(path_type=Path), help='Output results to JSON file')
@click.option('--verbose', '-v', is_flag=True, help='Show detailed output')
def scan(path: Path, recursive: bool, output: Path, verbose: bool):
    """Scan for ALS files and extract sample references."""
    parser = ALSParser()
    
    try:
        if path.is_file() and path.suffix.lower() == '.als':
            # Parse single file
            click.echo(f"Parsing single ALS file: {path}")
            result = parser.parse_als_file(path)
            results = [result]
        elif path.is_dir():
            # Scan directory
            click.echo(f"Scanning directory: {path}")
            if recursive:
                click.echo("Recursive scan enabled")
            
            als_files = parser.scan_directory(path, recursive)
            click.echo(f"Found {len(als_files)} ALS files")
            
            if not als_files:
                click.echo("No ALS files found.")
                return
            
            # Parse files with progress bar
            results = []
            with click.progressbar(als_files, label='Parsing ALS files', show_eta=True, show_percent=True) as bar:
                for als_file in bar:
                    try:
                        result = parser.parse_als_file(als_file)
                        results.append(result)
                    except Exception as e:
                        results.append({
                            'filename': als_file.name,
                            'path': str(als_file),
                            'error': str(e)
                        })
        else:
            click.echo("Error: Path must be an ALS file or directory", err=True)
            return
        
        # Display results
        total_references = 0
        for result in results:
            if 'error' not in result:
                total_references += result['reference_count']
                if verbose:
                    click.echo(f"\n📁 {result['filename']}")
                    click.echo(f"   Path: {result['path']}")
                    click.echo(f"   References: {result['reference_count']}")
                    if result['file_references']:
                        for ref in result['file_references']:
                            click.echo(f"   • {ref}")
                else:
                    click.echo(f"📁 {result['filename']} ({result['reference_count']} references)")
            else:
                click.echo(f"❌ {result['filename']}: {result['error']}", err=True)
        
        click.echo(f"\n✅ Processed {len(results)} files, found {total_references} total sample references")
        
        # Save to file if requested
        if output:
            with open(output, 'w') as f:
                json.dump(results, f, indent=2)
            click.echo(f"Results saved to: {output}")
            
    except Exception as e:
        click.echo(f"Error: {e}", err=True)


@main.command()
@click.argument('sample_name')
@click.argument('search_path', type=click.Path(exists=True, path_type=Path))
@click.option('--recursive/--no-recursive', default=True, help='Search directories recursively')
@click.option('--output', '-o', type=click.Path(path_type=Path), help='Output results to JSON file')
@click.option('--exact', is_flag=True, help='Exact filename match (case-sensitive)')
@click.option('--verbose', '-v', is_flag=True, help='Show detailed output including all sample references')
def search(sample_name: str, search_path: Path, recursive: bool, output: Path, exact: bool, verbose: bool):
    """Search for ALS projects containing a specific sample file."""
    parser = ALSParser()
    
    try:
        click.echo(f"🔍 Searching for sample: '{sample_name}'")
        click.echo(f"📂 Search path: {search_path}")
        if verbose:
            click.echo(f"🔄 Search mode: {'Exact match (case-sensitive)' if exact else 'Partial match (case-insensitive)'}")
            click.echo(f"🔄 Recursive: {'Yes' if recursive else 'No'}")
        
        # First scan for ALS files
        als_files = parser.scan_directory(search_path, recursive)
        if verbose:
            click.echo(f"📁 Found {len(als_files)} ALS files to search")
        
        if not als_files:
            click.echo("❌ No ALS files found in search path")
            return
        
        if not exact:
            # Make search case-insensitive by default
            if verbose:
                click.echo("🔍 Parsing files and searching for matches...")
            
            # Parse files individually with progress bar
            matches = []
            with click.progressbar(als_files, label='Parsing ALS files', show_eta=True, show_percent=True) as bar:
                for als_file in bar:
                    try:
                        result = parser.parse_als_file(als_file)
                        if 'error' not in result:
                            for file_ref in result['file_references']:
                                if sample_name.lower() in file_ref.lower():
                                    matches.append({
                                        **result,
                                        'matched_reference': file_ref
                                    })
                                    break
                        elif verbose:
                            click.echo(f"\n   ⚠️  Error parsing {result['filename']}: {result['error']}")
                    except Exception as e:
                        if verbose:
                            click.echo(f"\n   ⚠️  Error parsing {als_file.name}: {str(e)}")
            
            results = matches
        else:
            if verbose:
                click.echo("🔍 Performing exact match search...")
            results = parser.search_for_sample(sample_name, search_path, recursive)
        
        if not results:
            click.echo(f"❌ No projects found containing sample: '{sample_name}'")
            return
        
        click.echo(f"\n✅ Found {len(results)} project(s) containing '{sample_name}':")
        
        for result in results:
            click.echo(f"\n📁 {result['filename']}")
            click.echo(f"   Path: {result['path']}")
            click.echo(f"   Matched: {result['matched_reference']}")
            click.echo(f"   Total references: {result['reference_count']}")
            
            if verbose and result.get('file_references'):
                click.echo(f"   All sample references in this project:")
                for ref in result['file_references']:
                    if sample_name.lower() in ref.lower():
                        click.echo(f"   • {ref} ⭐")  # Highlight matches
                    else:
                        click.echo(f"   • {ref}")
        
        # Save to file if requested
        if output:
            with open(output, 'w') as f:
                json.dump(results, f, indent=2)
            click.echo(f"\nResults saved to: {output}")
            
    except Exception as e:
        click.echo(f"Error: {e}", err=True)


@main.command()
@click.argument('path', type=click.Path(exists=True, path_type=Path))
@click.option('--top', '-n', default=10, help='Show top N most referenced samples')
def stats(path: Path, top: int):
    """Show statistics about sample usage across ALS files."""
    parser = ALSParser()
    
    try:
        click.echo(f"📊 Analyzing sample usage in: {path}")
        
        if path.is_file() and path.suffix.lower() == '.als':
            als_files = [path]
        else:
            als_files = parser.scan_directory(path, recursive=True)
        
        if not als_files:
            click.echo("No ALS files found.")
            return
        
        results = parser.parse_multiple_files(als_files)
        
        # Count sample usage
        sample_counts = {}
        total_projects = 0
        total_references = 0
        
        for result in results:
            if 'error' not in result:
                total_projects += 1
                total_references += result['reference_count']
                for ref in result['file_references']:
                    sample_counts[ref] = sample_counts.get(ref, 0) + 1
        
        click.echo(f"\n📈 Statistics:")
        click.echo(f"   Total projects: {total_projects}")
        click.echo(f"   Total sample references: {total_references}")
        click.echo(f"   Unique samples: {len(sample_counts)}")
        
        if sample_counts:
            click.echo(f"\n🔥 Top {top} most used samples:")
            sorted_samples = sorted(sample_counts.items(), key=lambda x: x[1], reverse=True)
            for i, (sample, count) in enumerate(sorted_samples[:top], 1):
                click.echo(f"   {i:2d}. {sample} ({count} project{'s' if count != 1 else ''})")
        
    except Exception as e:
        click.echo(f"Error: {e}", err=True)


@main.command()
@click.argument('path', type=click.Path(exists=True, path_type=Path))
@click.option('--output', '-o', type=click.Path(path_type=Path), help='Output results to JSON file')
@click.option('--verbose', '-v', is_flag=True, help='Show detailed output including raw analysis')
@click.option('--plugin-filter', help='Filter by plugin name (case-insensitive partial match)')
def analyze_vsts(path: Path, output: Path, verbose: bool, plugin_filter: str):
    """Analyze VST plugins and extract preset information from ALS files."""
    parser = ALSParser()
    vst_analyzer = VSTPresetAnalyzer()
    
    try:
        if path.is_file() and path.suffix.lower() == '.als':
            als_files = [path]
            click.echo(f"🔍 Analyzing VST plugins in: {path}")
        elif path.is_dir():
            als_files = parser.scan_directory(path, recursive=True)
            click.echo(f"🔍 Analyzing VST plugins in directory: {path}")
            click.echo(f"📁 Found {len(als_files)} ALS files")
        else:
            click.echo("Error: Path must be an ALS file or directory", err=True)
            return
        
        if not als_files:
            click.echo("❌ No ALS files found")
            return
        
        all_results = []
        
        with click.progressbar(als_files, label='Analyzing VST plugins', show_eta=True, show_percent=True) as bar:
            for als_file in bar:
                try:
                    # Parse ALS file
                    xml_content = parser.decompress_als_file(als_file)
                    root = parser.parse_xml_content(xml_content)
                    
                    # Extract VST plugins
                    plugins = vst_analyzer.extract_vst_plugins(root)
                    
                    # Apply plugin filter if specified
                    if plugin_filter:
                        plugins = [p for p in plugins if plugin_filter.lower() in (p.get('plugin_name', '') or '').lower()]
                    
                    if plugins:
                        file_result = {
                            'file_path': str(als_file),
                            'file_name': als_file.name,
                            'plugin_count': len(plugins),
                            'plugins': plugins
                        }
                        all_results.append(file_result)
                        
                except Exception as e:
                    if verbose:
                        click.echo(f"\n⚠️  Error analyzing {als_file.name}: {e}")
        
        if not all_results:
            click.echo(f"❌ No VST plugins found" + (f" matching '{plugin_filter}'" if plugin_filter else ""))
            return
        
        # Display results
        total_plugins = sum(r['plugin_count'] for r in all_results)
        click.echo(f"\n✅ Found {total_plugins} VST plugin(s) across {len(all_results)} file(s)")
        
        for file_result in all_results:
            click.echo(f"\n📁 {file_result['file_name']} ({file_result['plugin_count']} plugins)")
            
            for plugin in file_result['plugins']:
                click.echo(f"\n  🎛️  {plugin.get('plugin_name', 'Unknown Plugin')}")
                click.echo(f"      File: {plugin.get('file_name', 'Unknown')}")
                click.echo(f"      ID: {plugin.get('unique_id', 'Unknown')}")
                click.echo(f"      Parameters: {plugin.get('num_parameters', 'Unknown')}")
                click.echo(f"      Programs: {plugin.get('num_programs', 'Unknown')}")
                
                if plugin.get('presets'):
                    click.echo(f"      Presets found: {len(plugin['presets'])}")
                    
                    for i, preset in enumerate(plugin['presets']):
                        click.echo(f"\n      📋 Preset {i + 1}:")
                        
                        # Show basic preset info
                        if preset.get('name'):
                            click.echo(f"         Name: {preset['name']}")
                        if preset.get('program_number') is not None:
                            click.echo(f"         Program: {preset['program_number']}")
                        if preset.get('buffer_size'):
                            click.echo(f"         Buffer size: {preset['buffer_size']} bytes")
                        
                        # Show extracted readable data
                        readable_data = preset.get('readable_data', {})
                        
                        # Embedded XML data (most important)
                        if 'embedded_xml' in readable_data:
                            xml_data = readable_data['embedded_xml']
                            click.echo(f"         🎯 Embedded XML found:")
                            
                            if xml_data.get('preset_name'):
                                click.echo(f"            Preset Name: {xml_data['preset_name']}")
                            if xml_data.get('plugin_name'):
                                click.echo(f"            Plugin: {xml_data['plugin_name']}")
                            if xml_data.get('plugin_version'):
                                click.echo(f"            Version: {xml_data['plugin_version']}")
                            if xml_data.get('setup_name'):
                                click.echo(f"            Setup: {xml_data['setup_name']}")
                            if xml_data.get('parameter_count'):
                                click.echo(f"            Parameters: {xml_data['parameter_count']}")
                            
                            if verbose and xml_data.get('parameter_values'):
                                click.echo(f"            Parameter values: {xml_data['parameter_values']}")
                        
                        # FXP format data
                        if 'fxp_format' in readable_data:
                            fxp_data = readable_data['fxp_format']
                            click.echo(f"         🎯 FXP format detected:")
                            if fxp_data.get('preset_name'):
                                click.echo(f"            Preset Name: {fxp_data['preset_name']}")
                            click.echo(f"            Plugin ID: {fxp_data.get('plugin_id', 'Unknown')}")
                            click.echo(f"            Parameters: {fxp_data.get('num_parameters', 'Unknown')}")
                        
                        # Readable strings
                        if 'strings' in readable_data and readable_data['strings']:
                            interesting_strings = [s for s in readable_data['strings'] 
                                                 if len(s) >= 8 and not s.isdigit()][:5]
                            if interesting_strings:
                                click.echo(f"         🔤 Readable strings: {', '.join(interesting_strings)}")
                        
                        # Show raw analysis in verbose mode
                        if verbose:
                            raw_analysis = preset.get('raw_analysis', {})
                            if raw_analysis.get('strategies_tried'):
                                click.echo(f"         🔧 Analysis strategies: {', '.join(raw_analysis['strategies_tried'])}")
        
        # Save to file if requested
        if output:
            with open(output, 'w') as f:
                json.dump(all_results, f, indent=2)
            click.echo(f"\n💾 Results saved to: {output}")
            
    except Exception as e:
        click.echo(f"Error: {e}", err=True)


if __name__ == '__main__':
    main()