# ha-nat-functions

### Local Testing

To test locally, install the AWS SAM CLI client
```
brew tap aws/tap
brew install aws-sam-cli
```

```
Build sam and invoke the functions
```
sam build
sam local invoke <FUNCTION NAME> -e <event_filename>.json
```

Example:

```
cd functions/replace-route
sam local invoke NATRouteTableFunction -e event.json
```

Making actual calls to AWS for testing
In on terminal 1
```
AWS_PROFILE=ChimeNonstable-Administrator aws sso login
```
In terminal 2
```
cd functions/replace-route
AWS_PROFILE=ChimeNonstable-Administrator sam build && AWS_PROFILE=ChimeNonstable-Administrator sam local start-lambda #This will start up a docker container running locally
```

To invoke the function back in terminal 1
```
AWS_PROFILE=ChimeNonstable-Administrator aws lambda invoke --function-name "NATRouteTableFunction" --endpoint-url "http://127.0.0.1:3001" --region us-east-1 --cli-binary-format raw-in-base64-out --payload file://functions/replace-route/sns-event.json --no-verify-ssl out.txt
```
