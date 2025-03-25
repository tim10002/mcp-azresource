"""Azure Resource Listing Tool"""

import os
import sys
import logging
import asyncio
import aiohttp
from dotenv import load_dotenv
from datetime import datetime
from mcp.server.fastmcp import FastMCP
from azure.identity.aio import ClientSecretCredential
from azure.mgmt.resource.resources.aio import ResourceManagementClient
from azure.core.exceptions import AzureError

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("azure_resource_lister")

# Initialize MCP
load_dotenv()
mcp = FastMCP(
    "azure-resource-lister",
    description="MCP server for listing Azure resources and costs",
    dependencies=["azure-identity", "python-dotenv", "azure-mgmt-resource", "aiohttp"],
)


async def get_azure_credential():
    """Get Azure credential using service principal."""
    tenant_id = os.getenv("AZURE_TENANT_ID")
    client_id = os.getenv("AZURE_CLIENT_ID")
    client_secret = os.getenv("AZURE_CLIENT_SECRET")
    
    if not all([tenant_id, client_id, client_secret]):
        raise ValueError("Missing Azure service principal credentials. Please set AZURE_TENANT_ID, AZURE_CLIENT_ID, and AZURE_CLIENT_SECRET environment variables.")
    
    return ClientSecretCredential(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret
    )


@mcp.tool()
async def list_azure_resources(subscription_id: str = None, resource_group_filter: str = None) -> str:
    """
    List Azure resource groups and resources using service principal authentication.
    
    Args:
        subscription_id (str, optional): Specific subscription ID to query. If not provided, will use default subscription.
        resource_group_filter (str, optional): Filter resource groups by name (case-insensitive contains match).
    
    Returns:
        str: Formatted markdown list of resource groups and their resources.
    """
    try:
        # Get Azure credential
        credential = await get_azure_credential()
        
        # Use provided subscription ID or get it from environment
        if not subscription_id:
            subscription_id = os.getenv("AZURE_SUBSCRIPTION_ID")
            if not subscription_id:
                return "Error: No subscription ID provided or found in AZURE_SUBSCRIPTION_ID environment variable."
        
        # Create resource client
        resource_client = ResourceManagementClient(credential, subscription_id)
        
        # Get resource groups
        resource_groups = []
        async for group in resource_client.resource_groups.list():
            if resource_group_filter and resource_group_filter.lower() not in group.name.lower():
                continue
            resource_groups.append(group)
        
        if not resource_groups:
            return f"No resource groups found in subscription '{subscription_id}'" + \
                  (f" matching filter '{resource_group_filter}'" if resource_group_filter else "")
        
        # Format result
        result = f"## Azure Resources in Subscription '{subscription_id}'\n\n"
        
        # Loop through resource groups
        for group in resource_groups:
            result += f"### Resource Group: {group.name}\n\n"
            result += f"- **Location**: {group.location}\n"
            if group.tags:
                result += f"- **Tags**: {', '.join([f'{k}={v}' for k, v in group.tags.items()])}\n"
            
            # Get resources in the group
            resources_found = False
            result += "\n**Resources:**\n\n"
            
            async for resource in resource_client.resources.list_by_resource_group(group.name):
                resources_found = True
                result += f"- **{resource.name}**\n"
                result += f"  - **Type**: {resource.type}\n"
                result += f"  - **Location**: {resource.location}\n"
                if hasattr(resource, "tags") and resource.tags:
                    result += f"  - **Tags**: {', '.join([f'{k}={v}' for k, v in resource.tags.items()])}\n"
                result += "\n"
            
            if not resources_found:
                result += "No resources found in this resource group.\n\n"
            
            result += "---\n\n"
        
        # Close the client
        await resource_client.close()
        
        return result
        
    except ValueError as ve:
        return f"Configuration Error: {str(ve)}"
    except AzureError as ae:
        return f"Azure Error: {str(ae)}"
    except Exception as e:
        logger.error(f"Error listing Azure resources: {str(e)}")
        return f"Error listing Azure resources: {str(e)}"


@mcp.tool()
async def get_azure_costs_rest(subscription_id: str = None, timeframe: str = "MonthToDate") -> str:
    """
    Get cost analysis data for an Azure subscription using REST API.
    
    Args:
        subscription_id (str, optional): Specific subscription ID to query. If not provided, uses default subscription.
        timeframe (str, optional): Time period for cost analysis.
    
    Returns:
        str: Formatted markdown with cost analysis data.
    """
    try:
        # Get Azure credential
        credential = await get_azure_credential()
        
        # Use provided subscription ID or get from environment
        if not subscription_id:
            subscription_id = os.getenv("AZURE_SUBSCRIPTION_ID")
            if not subscription_id:
                return "Error: No subscription ID provided or found in AZURE_SUBSCRIPTION_ID environment variable."
        
        # Get token from credential
        token = await credential.get_token("https://management.azure.com/.default")
        
        # Define the scope for the subscription
        scope = f"/subscriptions/{subscription_id}"
        
        # Define the API endpoint
        endpoint = f"https://management.azure.com{scope}/providers/Microsoft.CostManagement/query?api-version=2022-10-01"
        
        # Define the request payload
        payload = {
            "type": "ActualCost",
            "timeframe": timeframe,
            "dataset": {
                "granularity": "Daily",
                "aggregation": {
                    "totalCost": {
                        "name": "Cost",
                        "function": "Sum"
                    }
                }
            }
        }
        
        # Create headers with authorization
        headers = {
            "Authorization": f"Bearer {token.token}",
            "Content-Type": "application/json"
        }
        
        # Log request details
        logger.warning(f"Making REST API request to {endpoint}")
        logger.warning(f"Using timeframe: {timeframe}")
        
        # Make the REST API call
        async with aiohttp.ClientSession() as session:
            async with session.post(endpoint, json=payload, headers=headers) as response:
                if response.status != 200:
                    error_text = await response.text()
                    return f"Error from Azure API: Status {response.status}, Details: {error_text}"
                
                # Parse the response
                data = await response.json()
        
        # Format the results
        md_output = f"## Azure Cost Analysis for Subscription '{subscription_id}'\n\n"
        md_output += f"**Timeframe**: {timeframe}\n\n"
        md_output += "| Date | Cost | Currency |\n"
        md_output += "|------|------|----------|\n"
        
        # Check if we have any data
        rows = data.get("properties", {}).get("rows", [])
        
        if rows:
            total_cost = 0
            currency = ""
            
            for row in rows:
                try:
                    cost = float(row[0])
                    date_raw = row[1]
                    if isinstance(date_raw, float):
                        date_str = str(int(date_raw))
                        if len(date_str) == 8:
                            # 轉換為 YYYY-MM-DD 格式
                            year = date_str[:4]
                            month = date_str[4:6]
                            day = date_str[6:8]
                            date_value = f"{year}-{month}-{day}"
                        else:
                            date_value = str(date_raw)
                    else:
                        date_value = str(date_raw)
                        
                    curr = row[2]
                    total_cost += cost
                    currency = curr
                    md_output += f"| {date_value} | {cost:.1f} | {curr} |\n"
                except (IndexError, ValueError) as e:
                    logger.warning(f"Error processing row {row}: {str(e)}")
                    continue
            
            md_output += f"\n**Total Cost**: {total_cost:.1f} {currency}\n"
        else:
            md_output += "| No data | - | - |\n"
            md_output += "\nNo cost data available for the specified parameters.\n"
        
        return md_output
        
    except Exception as e:
        import traceback
        logger.error(f"Error retrieving Azure costs via REST API: {str(e)}")
        logger.error(traceback.format_exc())
        return f"Error retrieving Azure costs via REST API: {str(e)}"
    
if __name__ == "__main__":
    print(f"\n{'='*50}\nAzure Resource Lister MCP Server\nStarting server...\n{'='*50}\n")
    mcp.run()