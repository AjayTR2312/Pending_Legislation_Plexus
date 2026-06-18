# Deployment Commands — 2026-06-11

## 1. AWS Login

```bash
cd /home/m6119788/Pending_Legislation/cloud-tool/bin && ./cloud-tool --region us-east-1 login
# Select role [3]: tr-edl-adss-prod (664145726992): human-role/a209457-DataScientist
```

## 2. Build Docker Image

```bash
cd /home/m6119788/Pending_Legislation && docker build -t pending_legislation:<VERSION> .
```

## 3. Tag Docker Image

```bash
docker tag pending_legislation:<VERSION> 127288631409.dkr.ecr.us-east-1.amazonaws.com/a207870-ml-model-registry-model-registry-prod-use1:pending_legislation-<VERSION>
```

## 4. ECR Login

```bash
aws ecr get-login-password --region us-east-1 --profile tr-edl-adss-prod | docker login --username AWS --password-stdin 127288631409.dkr.ecr.us-east-1.amazonaws.com
```

## 5. Push to ECR

```bash
docker push 127288631409.dkr.ecr.us-east-1.amazonaws.com/a207870-ml-model-registry-model-registry-prod-use1:pending_legislation-<VERSION>
```

## Reference

- **Current latest version**: `3.1.1` — next would be `3.1.2`
- **ECR Image URI format**: `127288631409.dkr.ecr.us-east-1.amazonaws.com/a207870-ml-model-registry-model-registry-prod-use1:pending_legislation-<VERSION>`
- **ECR Repository ARN**: `arn:aws:ecr:us-east-1:127288631409:repository/a207870-ml-model-registry-model-registry-prod-use1`
- **Deployment URL**: `https://aideployment-prod.plexus.gcs.int.thomsonreuters.com/208321/pending_legislation/`
