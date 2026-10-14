# Deployment Commands - January 30, 2026

## Version: 1.0.19

This document contains all the deployment commands executed on January 30, 2026, with explanations for future reference.

---

## Prerequisites

### 1. Pull Latest Code from Repository
```bash
cd /home/m6121007/Pending_Legislation
git pull origin main
```
**Explanation**: Ensures local repository is up-to-date with the main branch before starting deployment.

### 2. Commit Local Changes
```bash
# Check current status
git status --short

# Commit UI changes
git add app/streamlit_app.py
git commit -m "Comment out Process(Vision) and Tag-Then-Clean buttons, rename Process(PDFPlumber) to Process with red styling"

# Commit dependency fix
git add requirements.txt
git commit -m "Add pdfplumber dependency to requirements.txt"
```
**Explanation**: 
- First commit: UI changes to hide Vision and Tag-Then-Clean buttons, rename PDFPlumber button to Process with red styling
- Second commit: Added missing `pdfplumber>=0.11.0` dependency that was causing ModuleNotFoundError

---

## AWS Authentication

### 3. Login to AWS Using Cloud-Tool
```bash
cd /home/m6121007/Pending_Legislation/cloud-tool/bin
./cloud-tool --region us-east-1 login
```

**Credentials Used**:
- Username: `MGMT\M6121007`
- Role Selected: `[3] tr-edl-adss-prod (664145726992): human-role/a209457-DataScientist`

**Explanation**: Refreshes AWS credentials needed for ECR operations. Credentials are stored in `~/.aws/credentials` under the `tr-edl-adss-prod` profile and expire after a certain period.

---

## Docker Build Process

### 4. Set JFrog Environment Variables
```bash
export TR_JFROG_USERNAME="6121007"
export TR_JFROG_TOKEN="<your-jfrog-token>"
```
**Explanation**: JFrog credentials are required to access private package repositories during Docker build. These are passed as build arguments to the Dockerfile.

### 5. Build Docker Image
```bash
cd /home/m6121007/Pending_Legislation

docker build --no-cache \
  -t pending_legislation \
  --build-arg TR_JFROG_USERNAME="$TR_JFROG_USERNAME" \
  --build-arg TR_JFROG_TOKEN="$TR_JFROG_TOKEN" \
  --file Dockerfile .
```

**Explanation**:
- `--no-cache`: Forces fresh build without using cached layers (ensures all changes are included)
- `-t pending_legislation`: Tags the image with name "pending_legislation:latest"
- `--build-arg`: Passes JFrog credentials to Docker build process for dependency installation
- `--file Dockerfile`: Specifies which Dockerfile to use
- `.`: Build context is current directory

**Build Time**: ~627 seconds (first build), ~283 seconds (second build after adding pdfplumber)

**Result**: Image ID created and tagged as `pending_legislation:latest`

---

## Local Testing

### 6. Test Docker Container Locally
```bash
# Remove any existing test container
docker rm -f pending_legislation_test

# Start test container on port 8505
docker run -d -p 8505:8501 \
  --name pending_legislation_test \
  pending_legislation:latest
```

**Explanation**:
- `-d`: Run container in detached mode (background)
- `-p 8505:8501`: Map host port 8505 to container port 8501 (Streamlit default)
- `--name`: Give container a friendly name for easy reference
- Container exposes Streamlit app on `http://localhost:8505`

### 7. Verify Container Health
```bash
# Wait for container to start
sleep 5

# Check health endpoint
curl -s http://localhost:8505/_stcore/health
# Expected output: "ok"

# Check HTTP status
curl -s -o /dev/null -w "HTTP Status: %{http_code}\n" http://localhost:8505/
# Expected output: "HTTP Status: 200"
```

**Explanation**: Streamlit provides a health check endpoint at `/_stcore/health` that returns "ok" when the app is running properly.

### 8. Verify Critical Configuration
```bash
# Check Streamlit configuration
docker exec pending_legislation_test cat .streamlit/config.toml | grep -E "maxUploadSize|enableWebsocket|enableXsrf"
```

**Expected Output**:
```
maxUploadSize = 1000
enableXsrfProtection = false
enableWebsocketCompression = false
```

**Explanation**: Verifies critical settings are correctly configured:
- `maxUploadSize = 1000`: Allows uploads up to 1000 MB (fixes 403 errors)
- `enableXsrfProtection = false`: Disables CSRF protection for internal use
- `enableWebsocketCompression = false`: Prevents websocket compression issues

### 9. Check for Module Errors
```bash
# Check container logs for errors
docker logs pending_legislation_test 2>&1 | grep -i "pdfplumber\|error\|modulenotfound" | head -20
```

**Explanation**: Searches container logs for any module errors or exceptions. This is how we discovered the missing `pdfplumber` dependency.

### 10. Clean Up Test Container
```bash
docker stop pending_legislation_test
docker rm pending_legislation_test
```

**Explanation**: Removes test container to free up resources and port 8505.

---

## ECR Deployment

### 11. Tag Image for ECR
```bash
docker tag pending_legislation:latest \
  127288631409.dkr.ecr.us-east-1.amazonaws.com/a207870-ml-model-registry-model-registry-prod-use1:pending_legislation-1.0.19
```

**Explanation**:
- Tags the local image with the full ECR repository path
- Version number: `1.0.19` (incremented from previous version 1.0.18)
- ECR Repository: `a207870-ml-model-registry-model-registry-prod-use1`
- AWS Account: `127288631409`
- Region: `us-east-1`

### 12. Authenticate with ECR
```bash
aws ecr get-login-password --region us-east-1 --profile tr-edl-adss-prod | \
  docker login --username AWS --password-stdin \
  127288631409.dkr.ecr.us-east-1.amazonaws.com
```

**Expected Output**:
```
WARNING! Your credentials are stored unencrypted in '/home/m6121007/.docker/config.json'.
Configure a credential helper to remove this warning. See
https://docs.docker.com/go/credential-store/

Login Succeeded
```

**Explanation**:
- Uses AWS CLI to get temporary ECR login password
- `--profile tr-edl-adss-prod`: Uses the AWS profile created during cloud-tool login
- Pipes password to `docker login` command
- Authentication is required before pushing images to ECR

### 13. Push Image to ECR
```bash
docker push \
  127288631409.dkr.ecr.us-east-1.amazonaws.com/a207870-ml-model-registry-model-registry-prod-use1:pending_legislation-1.0.19
```

**Expected Output**:
```
The push refers to repository [127288631409.dkr.ecr.us-east-1.amazonaws.com/a207870-ml-model-registry-model-registry-prod-use1]
b1f56918f491: Pushed 
...
pending_legislation-1.0.19: digest: sha256:bd03313be96d852cd5a1507d5d7aa7a6f3108481f23a5cde89c223844cbf7e32 size: 856
```

**Explanation**: 
- Pushes all image layers to ECR
- Some layers show "Layer already exists" (reused from previous versions)
- Returns final digest (SHA256 hash) for image verification

---

## Production Verification

### 14. Test Production Image from ECR
```bash
# Pull and run production image on different port
docker run -d -p 8506:8501 \
  --name pending_legislation_production_test \
  127288631409.dkr.ecr.us-east-1.amazonaws.com/a207870-ml-model-registry-model-registry-prod-use1:pending_legislation-1.0.19
```

**Explanation**: Tests the actual image from ECR (not local build) to ensure the push was successful and image is accessible.

### 15. Verify Production Image
```bash
# Check health
curl -s http://localhost:8506/_stcore/health
# Expected: "ok"

# Verify config
docker exec pending_legislation_production_test cat .streamlit/config.toml | grep maxUploadSize
# Expected: maxUploadSize = 1000
```

### 16. Clean Up Production Test
```bash
docker stop pending_legislation_production_test
docker rm pending_legislation_production_test
```

---

## Deployment Summary

### Final Image Details

**Image URI**:
```
127288631409.dkr.ecr.us-east-1.amazonaws.com/a207870-ml-model-registry-model-registry-prod-use1:pending_legislation-1.0.19
```

**Image Digest**:
```
sha256:bd03313be96d852cd5a1507d5d7aa7a6f3108481f23a5cde89c223844cbf7e32
```

**Image ARN**:
```
arn:aws:ecr:us-east-1:127288631409:repository/a207870-ml-model-registry-model-registry-prod-use1@sha256:bd03313be96d852cd5a1507d5d7aa7a6f3108481f23a5cde89c223844cbf7e32
```

### Changes in Version 1.0.19

1. **UI Changes** (app/streamlit_app.py):
   - Commented out "🚀 Process (Vision)" button
   - Commented out "🏷️ Tag-Then-Clean" button
   - Renamed "📄 Process (PDFPlumber)" to "🚀 Process"
   - Changed Process button color to red with custom CSS

2. **Dependency Fix** (requirements.txt):
   - Added `pdfplumber>=0.11.0` to fix ModuleNotFoundError

### Validation Checklist

- [x] Docker build completes without errors
- [x] Health check returns "ok"
- [x] Config file in correct location (`.streamlit/config.toml`)
- [x] maxUploadSize = 1000 MB
- [x] enableWebsocketCompression = false
- [x] enableXsrfProtection = false
- [x] pdfplumber module installed and working
- [x] Session directory writable
- [x] Image pushed to ECR successfully
- [x] Production image tested from ECR
- [x] Digest confirmed

---

## Troubleshooting Commands Reference

### View Docker Logs
```bash
docker logs <container_name>
docker logs <container_name> 2>&1 | grep -i "error"
```

### Inspect Container
```bash
docker inspect <container_name>
docker exec -it <container_name> /bin/bash
```

### List Containers and Images
```bash
docker ps                    # Running containers
docker ps -a                 # All containers
docker images                # All images
docker images | grep pending_legislation
```

### Clean Up
```bash
# Remove specific container
docker stop <container_name>
docker rm <container_name>

# Force remove
docker rm -f <container_name>

# Remove old images
docker rmi <image_id>

# Prune unused resources
docker container prune
docker image prune
docker system prune
```

### AWS/ECR Commands
```bash
# List ECR repositories
aws ecr describe-repositories --region us-east-1 --profile tr-edl-adss-prod

# List images in repository
aws ecr list-images \
  --repository-name a207870-ml-model-registry-model-registry-prod-use1 \
  --region us-east-1 \
  --profile tr-edl-adss-prod

# Describe specific image
aws ecr describe-images \
  --repository-name a207870-ml-model-registry-model-registry-prod-use1 \
  --image-ids imageTag=pending_legislation-1.0.19 \
  --region us-east-1 \
  --profile tr-edl-adss-prod
```

---

## Notes

1. **Always test locally** before pushing to ECR
2. **Verify health endpoint** after container startup
3. **Check for module errors** in container logs
4. **AWS credentials expire** - refresh before ECR operations
5. **Build args required** - TR_JFROG_USERNAME and TR_JFROG_TOKEN must be set
6. **Version numbering** - Increment version for each deployment
7. **Image size** - Monitor layer sizes to optimize build time
8. **Port conflicts** - Use different ports (8505, 8506) when testing multiple containers

---

## Quick Reference: One-Command Deployment

```bash
# 1. Set credentials
export TR_JFROG_USERNAME="6121007"
export TR_JFROG_TOKEN="<your-jfrog-token>"

# 2. Build
docker build --no-cache -t pending_legislation --build-arg TR_JFROG_USERNAME="$TR_JFROG_USERNAME" --build-arg TR_JFROG_TOKEN="$TR_JFROG_TOKEN" --file Dockerfile .

# 3. Tag (update version number)
docker tag pending_legislation:latest 127288631409.dkr.ecr.us-east-1.amazonaws.com/a207870-ml-model-registry-model-registry-prod-use1:pending_legislation-1.0.19

# 4. Login to ECR
aws ecr get-login-password --region us-east-1 --profile tr-edl-adss-prod | docker login --username AWS --password-stdin 127288631409.dkr.ecr.us-east-1.amazonaws.com

# 5. Push
docker push 127288631409.dkr.ecr.us-east-1.amazonaws.com/a207870-ml-model-registry-model-registry-prod-use1:pending_legislation-1.0.19
```

---

**Deployment Date**: January 30, 2026  
**Deployed By**: M6121007  
**Status**: ✅ Successfully Deployed  
**Version**: 1.0.19
