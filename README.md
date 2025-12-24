# PDF-to-Text Conversion Application - Deployment Guide

## Overview
This is a comprehensive PDF-to-Text conversion application with multi-state support, featuring Claude Opus 4 LLM integration, DOCX analysis, and automated tag verification.

## Features
- **Multi-State Support**: 28 states with state-specific processing rules
- **LLM Integration**: Claude Opus 4 for intelligent PDF conversion
- **DOCX Analysis**: Adobe PDF Services for format analysis
- **Tag Verification**: Automated verification with state-specific prompts
- **Docker Support**: Complete containerization for easy deployment
- **Example-Based Learning**: Few-shot learning with state examples
- **Web Interface**: Streamlit-based GUI for easy file management and processing
- **Real-time Monitoring**: Live processing logs and progress tracking

## Folder Structure

```
Temp/
├── app/                          # Application code
│   ├── pdf_to_text_llm.py       # Main application
│   ├── pdf_docx_analysis_pipeline.py
│   ├── llm_tag_verifier.py
│   └── geanai.py
├── config/                       # Configuration files
│   ├── requirements.txt
│   └── pdfservices-api-credentials.json.template
├── prompts/                      # State-specific prompts
│   ├── conversion/              # PDF conversion prompts
│   └── verification/            # Tag verification prompts
├── data/                        # Data directories (volume mounted)
│   ├── input/                   # Input PDF files by state
│   ├── output/                  # Output text files by state
│   ├── examples/                # Example files for few-shot learning
│   └── logs/                    # Application logs
├── docker/                      # Docker configuration
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── .dockerignore
│   └── docker-manage.bat        # Management script
└── docs/                        # Documentation
    └── README.md               # This file
```

## Supported States (28 Total)
- AR (Arkansas), CA (California), CO (Colorado), CT (Connecticut)
- DC (District of Columbia), DE (Delaware), FL (Florida), GA (Georgia)
- HI (Hawaii), IA (Iowa), ID (Idaho), IL (Illinois)
- IN (Indiana), KS (Kansas), ME (Maine), MN (Minnesota)
- NC (North Carolina), ND (North Dakota), NE (Nebraska), NY (New York)
- OH (Ohio), OK (Oklahoma), SD (South Dakota), TN (Tennessee)
- TX (Texas), VT (Vermont), WI (Wisconsin), WY (Wyoming)

## Quick Start

### Prerequisites
- Docker Desktop installed and running
- Adobe PDF Services credentials
- Access to Thomson Reuters Anthropic API

### Setup Instructions

1. **Clone/Copy this Temp folder to your deployment location**

2. **Configure Adobe PDF Services**:
   ```bash
   cd config
   cp pdfservices-api-credentials.json.template pdfservices-api-credentials.json
   # Edit the file with your actual Adobe credentials
   ```

3. **Build and Start the Application**:
   ```bash
   cd docker
   docker-manage.bat build
   docker-manage.bat start
   ```

4. **Access the Application**:
   - **Web Interface**: Open your browser to `http://localhost:8501`
   - **Command Line**: Use `docker exec -it pdf-to-text-converter python pdf_to_text_llm.py`
   - Place PDF files in `data/input/[STATE]/` folders
   - Results will appear in `data/output/[STATE]/` folders

### Web Interface Features

The Streamlit web interface provides:
- **File Management**: Easy upload and organization by state
- **Real-time Processing**: Live progress tracking with visual indicators
- **File Browser**: View and download all generated files
- **Analytics Dashboard**: Processing statistics and metrics
- **Session Logs**: Real-time activity monitoring
- **Tag Visualization**: Highlighted legislative tags in verified files

### Docker Management

Use the provided management script for easy Docker operations:

```bash
# Build the Docker image
docker-manage.bat build

# Start the application
docker-manage.bat start

# Check application status
docker-manage.bat status

# View logs
docker-manage.bat logs

# Stop the application
docker-manage.bat stop

# Restart the application
docker-manage.bat restart

# Clean up (remove containers and images)
docker-manage.bat cleanup

# Show help
docker-manage.bat help
```

## Configuration

### Adobe PDF Services
Edit `config/pdfservices-api-credentials.json` with your credentials:
```json
{
  "client_credentials": {
    "client_id": "your_client_id",
    "client_secret": "your_client_secret"
  }
}
```

### State-Specific Processing
- **Conversion Prompts**: Located in `prompts/conversion/`
- **Verification Prompts**: Located in `prompts/verification/`
- **Examples**: Place in `data/examples/[STATE]/` for few-shot learning

## Data Persistence

The following directories are volume-mounted for data persistence:
- `data/input/` - Input PDF files
- `data/output/` - Output text files
- `data/examples/` - Example files for training
- `data/logs/` - Application logs

Data persists even when containers are stopped/removed.

## Processing Workflow

### Web Interface (Recommended)
1. **Access**: Open `http://localhost:8501` in your browser
2. **State Selection**: Choose your state from the sidebar
3. **File Upload**: Upload PDF files using the web interface
4. **Processing**: Click "Start Processing" and monitor real-time progress
5. **Review**: Browse generated files with the built-in viewer
6. **Download**: Get results directly through the web interface

### Command Line Interface
1. **PDF Conversion**: Claude Opus 4 converts PDF to text using state-specific prompts
2. **DOCX Analysis**: Adobe PDF Services creates DOCX for format analysis
3. **Tag Verification**: LLM verifies and corrects formatting tags
4. **Output Generation**: Produces verified text files with reports

## State-Specific Features

### Connecticut (CT)
- Underlined text → `[A>text<A]`
- Bracketed text → `[D>text<D]`

### Indiana (IN)
- Bold text starting from "SECTION 1" → `[A>text<A]`

### Kansas (KS)
- Multi-format tagging (underlined/bold/italic → `[A>text<A]`)
- Strikethrough → `[D>text<D]`
- Excludes lines 2-3 from processing

### Florida (FL)
- Underlined text → `[A>text<A]`
- Strikethrough → `[D>text<D]`

## Troubleshooting

### Common Issues

1. **Docker not running**:
   ```
   Error: Docker is not running
   Solution: Start Docker Desktop
   ```

2. **Port 8501 already in use**:
   ```
   Solution: Stop other Streamlit applications or change port in docker-compose.yml
   ```

3. **Adobe credentials not found**:
   ```
   Solution: Ensure pdfservices-api-credentials.json is properly configured
   ```

### Logs
Check application logs for detailed error information:
```bash
docker-manage.bat logs
```

### Volume Issues
If you encounter volume mounting issues:
```bash
docker-manage.bat cleanup
docker-manage.bat build
docker-manage.bat start
```

## Development

### Adding New States
1. Add state to `AVAILABLE_STATES` in `pdf_to_text_llm.py`
2. Create state-specific prompts in `prompts/conversion/` and `prompts/verification/`
3. Add example files to `data/examples/[STATE]/`
4. Update directory creation in Dockerfile

### Custom Prompts
- Conversion prompts: Use `{DOCUMENT_CONTENT}` placeholder
- Verification prompts: Use `{SUMMARY_CONTENT}` and `{OUTPUT_TEXT_CONTENT}` placeholders

## Performance

### Resource Requirements
- **Memory**: 2GB minimum, 4GB recommended
- **CPU**: 2 cores minimum
- **Disk**: 10GB for application + space for your documents

### Scaling
- For high-volume processing, consider increasing Docker memory limits
- Use external databases for metadata storage in production
- Implement queue systems for batch processing

## Security

### API Keys
- Adobe PDF Services credentials stored in `config/` (not in Docker image)
- Thomson Reuters workspace ID configured in application code
- All credentials volume-mounted as read-only

### Network
- Application runs on localhost:8501 by default
- For production, use reverse proxy with SSL termination

## Support

### Logs Location
- Container logs: `docker-manage.bat logs`
- Application logs: `data/logs/`

### Health Checks
- Built-in Docker health checks
- Endpoint monitoring on port 8501

## Deployment Notes

This deployment structure provides:
- ✅ Clean separation of code, config, and data
- ✅ Easy Docker management
- ✅ Data persistence
- ✅ State-specific processing
- ✅ Comprehensive logging
- ✅ Volume-mounted configuration
- ✅ Production-ready structure

For production deployment, consider:
- Adding SSL/TLS termination
- Implementing proper logging aggregation
- Adding monitoring and alerting
- Using external storage for large files
- Implementing backup strategies for persistent data