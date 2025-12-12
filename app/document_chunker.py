"""
Document Chunker for Large Document Verification
Handles splitting of both text and summary items based on paragraph locations.
"""

import re
import logging
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional

@dataclass
class TextChunk:
    """Represents a chunk of text with metadata."""
    content: str
    chunk_number: int
    total_chunks: int
    para_start: int  # Global paragraph number where chunk starts
    para_end: int    # Global paragraph number where chunk ends
    token_estimate: int
    
    def __repr__(self):
        return f"Chunk {self.chunk_number}/{self.total_chunks} (Para {self.para_start}-{self.para_end}, ~{self.token_estimate} tokens)"


@dataclass
class SummaryItem:
    """Represents a single item from the summary file."""
    word: str
    para: int
    run: int
    context: str
    formatting_type: str
    full_line: str
    
    def __repr__(self):
        return f"'{self.word}' @ Para {self.para}, Run {self.run}"


class DocumentChunker:
    """
    Chunks large documents for verification while maintaining paragraph location references.
    
    This chunker:
    1. Splits text into manageable chunks based on token limits
    2. Tracks global paragraph numbers across chunks
    3. Splits summary items to match text chunks
    """
    
    def __init__(self, max_tokens: int = 50000, tokens_per_char: float = 0.25):
        """
        Initialize the document chunker.
        
        Args:
            max_tokens: Maximum tokens per chunk (default: 50000 for verification)
            tokens_per_char: Estimated tokens per character for rough estimation
        """
        self.max_tokens = max_tokens
        self.tokens_per_char = tokens_per_char
        logging.info(f"DocumentChunker initialized with max_tokens={max_tokens}")
    
    def estimate_tokens(self, text: str) -> int:
        """
        Estimate token count from text length.
        
        Args:
            text: Text to estimate
            
        Returns:
            Estimated token count
        """
        return int(len(text) * self.tokens_per_char)
    
    def split_into_paragraphs(self, text: str) -> List[str]:
        """
        Split text into paragraphs based on double newlines.
        
        Args:
            text: Full text to split
            
        Returns:
            List of paragraph strings
        """
        # Split on double newlines (paragraph breaks)
        paragraphs = re.split(r'\n\s*\n', text)
        
        # Filter out empty paragraphs
        paragraphs = [p.strip() for p in paragraphs if p.strip()]
        
        logging.info(f"Split text into {len(paragraphs)} paragraphs")
        return paragraphs
    
    def chunk_text(self, text: str) -> List[TextChunk]:
        """
        Split text into chunks while tracking global paragraph numbers.
        
        Args:
            text: Full text to chunk
            
        Returns:
            List of TextChunk objects with paragraph range metadata
        """
        paragraphs = self.split_into_paragraphs(text)
        
        if not paragraphs:
            logging.warning("No paragraphs found in text")
            return []
        
        chunks = []
        current_chunk_paras = []
        current_tokens = 0
        global_para_num = 1
        chunk_start_para = 1
        
        for para in paragraphs:
            para_tokens = self.estimate_tokens(para)
            
            # Check if adding this paragraph would exceed token limit
            if current_tokens + para_tokens > self.max_tokens and current_chunk_paras:
                # Finalize current chunk
                chunk_content = '\n\n'.join(current_chunk_paras)
                chunks.append(TextChunk(
                    content=chunk_content,
                    chunk_number=len(chunks) + 1,
                    total_chunks=0,  # Will be updated later
                    para_start=chunk_start_para,
                    para_end=global_para_num - 1,
                    token_estimate=current_tokens
                ))
                
                # Reset for next chunk
                current_chunk_paras = []
                current_tokens = 0
                chunk_start_para = global_para_num
            
            # Add paragraph to current chunk
            current_chunk_paras.append(para)
            current_tokens += para_tokens
            global_para_num += 1
        
        # Add final chunk if there's content
        if current_chunk_paras:
            chunk_content = '\n\n'.join(current_chunk_paras)
            chunks.append(TextChunk(
                content=chunk_content,
                chunk_number=len(chunks) + 1,
                total_chunks=0,
                para_start=chunk_start_para,
                para_end=global_para_num - 1,
                token_estimate=current_tokens
            ))
        
        # Update total_chunks for all chunks
        total = len(chunks)
        for chunk in chunks:
            chunk.total_chunks = total
        
        logging.info(f"Created {total} chunks from {global_para_num - 1} paragraphs")
        for chunk in chunks:
            logging.info(f"  {chunk}")
        
        return chunks
    
    def parse_summary_items(self, summary_content: str) -> List[SummaryItem]:
        """
        Parse summary file to extract items with Para/Run locations.
        
        Expected format:
        1. 'word'
           Location: Para 55, Run 3: ...context...
           Formatting: Bold
        
        NOTE: Para/Run numbers come from DOCX structure and may not match plain text paragraph count.
        Use context matching as primary method, Para/Run as secondary hint.
        
        Args:
            summary_content: Content of the summary file
            
        Returns:
            List of SummaryItem objects
        """
        items = []
        lines = summary_content.split('\n')
        
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            
            # Look for numbered item lines like "1. 'word'"
            item_match = re.match(r'(\d+)\.\s*[\'"](.+?)[\'"]', line)
            if item_match:
                item_num = item_match.group(1)
                word = item_match.group(2)
                
                # Look for Location line (next line)
                if i + 1 < len(lines):
                    location_line = lines[i + 1].strip()
                    location_match = re.search(r'Para\s+(\d+),\s+Run\s+(\d+):\s*(.+)', location_line)
                    
                    if location_match:
                        para_num = int(location_match.group(1))
                        run_num = int(location_match.group(2))
                        context = location_match.group(3).strip()
                        
                        # Look for Formatting line (next line after location)
                        formatting_type = "Unknown"
                        if i + 2 < len(lines):
                            formatting_line = lines[i + 2].strip()
                            format_match = re.search(r'Formatting:\s*(.+)', formatting_line)
                            if format_match:
                                formatting_type = format_match.group(1).strip()
                        
                        items.append(SummaryItem(
                            word=word,
                            para=para_num,
                            run=run_num,
                            context=context,
                            formatting_type=formatting_type,
                            full_line=f"{item_num}. '{word}'\n   {location_line}\n   Formatting: {formatting_type}"
                        ))
                        
                        logging.debug(f"Parsed item: '{word}' @ Para {para_num}, Run {run_num}")
            
            i += 1
        
        logging.info(f"Parsed {len(items)} summary items")
        logging.warning(f"NOTE: Para/Run numbers are from DOCX structure, not plain text paragraphs")
        return items
    
    def assign_items_to_chunks(
        self, 
        summary_items: List[SummaryItem], 
        text_chunks: List[TextChunk]
    ) -> List[Tuple[TextChunk, List[SummaryItem]]]:
        """
        Assign summary items to chunks using context-based matching.
        
        Since Para/Run numbers from DOCX don't match plain text structure,
        we use context matching: search for the word in the context within each chunk.
        
        Args:
            summary_items: List of parsed summary items
            text_chunks: List of text chunks with paragraph ranges
            
        Returns:
            List of (TextChunk, relevant_items) tuples
        """
        assignments = []
        
        logging.info("Assigning items to chunks using context-based matching...")
        
        for chunk in text_chunks:
            # Find items whose context appears in this chunk
            relevant_items = []
            
            for item in summary_items:
                # Extract the marked word from context (between 【 and 】)
                context_pattern = item.context
                
                # Remove the markers to get plain context
                plain_context = context_pattern.replace('【', '').replace('】', '')
                
                # Check if this context appears in the chunk
                # Use a fuzzy match - just check if the word and some surrounding text appears
                if item.word in chunk.content and plain_context[:50] in chunk.content:
                    relevant_items.append(item)
            
            assignments.append((chunk, relevant_items))
            
            logging.info(f"Chunk {chunk.chunk_number}/{chunk.total_chunks}: "
                        f"{len(relevant_items)} items matched by context")
        
        # Log summary statistics
        total_assigned = sum(len(items) for _, items in assignments)
        unassigned_count = len(summary_items) - total_assigned
        
        if unassigned_count > 0:
            logging.warning(f"{unassigned_count} items could not be matched to any chunk")
            logging.warning("This may happen if the text was heavily modified after analysis")
        else:
            logging.info(f"✓ All {len(summary_items)} items successfully assigned")
        
        return assignments
    
    def create_mini_summary(self, items: List[SummaryItem]) -> str:
        """
        Create a mini-summary string from a subset of items.
        
        Args:
            items: List of summary items to include
            
        Returns:
            Formatted summary string for this chunk
        """
        if not items:
            return "No formatted items to verify in this chunk."
        
        lines = ["Formatted items with contextual locations:\n"]
        
        for i, item in enumerate(items, 1):
            lines.append(f"{i}. '{item.word}'")
            lines.append(f"   Location: Para {item.para}, Run {item.run}: {item.context}")
            lines.append(f"   Formatting: {item.formatting_type}")
            lines.append("")
        
        return '\n'.join(lines)
    
    def should_chunk_document(self, text: str, summary_content: str) -> Tuple[bool, int, int]:
        """
        Determine if a document needs chunking based on estimated token count.
        
        Args:
            text: The output text content
            summary_content: The summary content
            
        Returns:
            Tuple of (needs_chunking, text_tokens, total_tokens)
        """
        text_tokens = self.estimate_tokens(text)
        summary_tokens = self.estimate_tokens(summary_content)
        total_tokens = text_tokens + summary_tokens
        
        # Add buffer for prompt template (rough estimate: 2000 tokens)
        total_tokens += 2000
        
        # Claude Opus 4 has 200K input token limit
        # But for verification, we want to stay well under to avoid issues
        # Use 150K as safe threshold
        needs_chunking = total_tokens > 150000
        
        logging.info(f"Token estimation: text={text_tokens:,}, summary={summary_tokens:,}, "
                    f"total={total_tokens:,}, needs_chunking={needs_chunking}")
        
        return needs_chunking, text_tokens, total_tokens


def test_chunker():
    """Test function for DocumentChunker."""
    # Sample text with paragraphs
    test_text = """Section 1. This is the first paragraph.
It continues on multiple lines.

Section 2. This is the second paragraph.
With more content here.

Section 3. Third paragraph with some content.

Section 4. Fourth paragraph continues.

Section 5. Fifth paragraph here."""

    # Sample summary
    test_summary = """1. 'first'
   Location: Para 1, Run 2: ...the first paragraph...
   Formatting: Bold

2. 'second'
   Location: Para 2, Run 1: ...the second paragraph...
   Formatting: Italic

3. 'fifth'
   Location: Para 5, Run 1: ...Fifth paragraph...
   Formatting: Underline"""

    # Test chunking
    chunker = DocumentChunker(max_tokens=100)  # Small limit for testing
    
    print("\n=== Testing Text Chunking ===")
    chunks = chunker.chunk_text(test_text)
    for chunk in chunks:
        print(f"\n{chunk}")
        print(f"Content preview: {chunk.content[:100]}...")
    
    print("\n=== Testing Summary Parsing ===")
    items = chunker.parse_summary_items(test_summary)
    for item in items:
        print(f"  {item}")
    
    print("\n=== Testing Item Assignment ===")
    assignments = chunker.assign_items_to_chunks(items, chunks)
    for chunk, chunk_items in assignments:
        print(f"\n{chunk}")
        print(f"  Items: {[item.word for item in chunk_items]}")
        if chunk_items:
            mini_summary = chunker.create_mini_summary(chunk_items)
            print(f"  Mini-summary:\n{mini_summary}")


if __name__ == "__main__":
    # Set up logging
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    test_chunker()
