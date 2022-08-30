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
cd functions/route-table
sam local invoke NATRouteTableFunction -e event.json
```