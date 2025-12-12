#!/usr/bin/env python3
"""
Tag Remover Tool

This script removes [A>...<A] and [D>...<D] tags from text files,
leaving only the clean plain text content.

Usage:
    python tag_remover.py input_file.txt [output_file.txt]
    
If no output file is specified, it will create a new file with "_clean" suffix.
"""

import re
import sys
import os
from pathlib import Path

def remove_tags(text):
    """
    Remove all [A>...<A] and [D>...<D] tags from text, keeping only the content inside.
    
    Args:
        text (str): Input text with tags
        
    Returns:
        str: Clean text with tags removed
    """
    # Remove [A>content<A] tags - keep the content
    text = re.sub(r'\[A>(.*?)<A\]', r'\1', text)
    
    # Remove [D>content<D] tags - keep the content  
    text = re.sub(r'\[D>(.*?)<D\]', r'\1', text)
    
    return text

def clean_file(input_file, output_file=None):
    """
    Clean tags from a file and save to output file.
    
    Args:
        input_file (str): Path to input file
        output_file (str, optional): Path to output file. If None, creates "_clean" version.
        
    Returns:
        str: Path to output file
    """
    input_path = Path(input_file)
    
    # Generate output filename if not provided
    if output_file is None:
        output_path = input_path.parent / f"{input_path.stem}_clean{input_path.suffix}"
    else:
        output_path = Path(output_file)
    
    try:
        # Read input file
        print(f"📖 Reading: {input_file}")
        with open(input_path, 'r', encoding='utf-8') as f:
            original_text = f.read()
        
        # Count original tags
        a_tags = len(re.findall(r'\[A>.*?<A\]', original_text))
        d_tags = len(re.findall(r'\[D>.*?<D\]', original_text))
        total_tags = a_tags + d_tags
        
        print(f"🏷️  Found {total_tags} tags ([A>: {a_tags}, [D>: {d_tags})")
        
        # Remove tags
        clean_text = remove_tags(original_text)
        
        # Write output file
        print(f"💾 Writing: {output_path}")
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(clean_text)
        
        # Show results
        print(f"✅ Successfully cleaned file!")
        print(f"📊 Original size: {len(original_text)} characters")
        print(f"📊 Clean size: {len(clean_text)} characters")
        print(f"📊 Removed {total_tags} tags")
        
        return str(output_path)
        
    except FileNotFoundError:
        print(f"❌ Error: File not found - {input_file}")
        return None
    except Exception as e:
        print(f"❌ Error processing file: {e}")
        return None

def test_tag_removal():
    """Test the tag removal function with sample text."""
    test_text = """
    This is normal text.
    
    This has [A>added content<A] in the middle.
    
    This has [D>deleted content<D] to remove.
    
    This has [A>multiple<A] [A>added<A] tags and [D>some<D] [D>deleted<D] ones.
    
    This has nested content like [A>text message, or voicemail transmission<A].
    """
    
    print("🧪 Testing tag removal...")
    print("\nOriginal text:")
    print("-" * 50)
    print(test_text)
    
    clean_text = remove_tags(test_text)
    
    print("\nCleaned text:")
    print("-" * 50)
    print(clean_text)
    
    return clean_text

def main():
    """Main function to handle command line usage."""
    if len(sys.argv) < 2:
        print("Usage: python tag_remover.py input_file.txt [output_file.txt]")
        print("\nRunning test mode instead...")
        test_tag_removal()
        return
    
    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None
    
    if not os.path.exists(input_file):
        print(f"❌ Error: Input file does not exist - {input_file}")
        return
    
    result = clean_file(input_file, output_file)
    if result:
        print(f"\n🎯 Cleaned file saved as: {result}")

if __name__ == "__main__":
    main()