Restaurent_results = {
    "expensive_south": {
        "name": "The restaurant is called Chiquito Restaurant Bar.",
        "address": "It is located at 2G Cambridge Leisure Park, Cherry Hinton Road, Cherry Hinton, CB1 7DY.",
        "phone": "You can contact them at 01223 400170."
    },
    "cheap_south": {
        "name": "The restaurant is called The Cambridge Blue.",
        "address": "It is located at 85-87, Gwydir Street, CB1 2LG.",
        "phone": "You can contact them at 01223 357268."
    },
    "cheap_north": {
        "name": "The restaurant is called The Eagle.",
        "address": "It is located at 8, Bene't Street, CB2 3QN.",
        "phone": "You can contact them at 01223 505 020."
    },
    "expensive_north": {
        "name": "The restaurant is called The Oak Bistro.",
        "address": "It is located at 1, St Marys Passage, CB2 3PQ.",
        "phone": "You can contact them at 01223 352 999."
    },
}

Theater_results = {
    "expensive_south": {
        "name": "The theater is called Cambridge Arts Theatre.",
        "address": "It is located at 6 St Edward's Passage, CB2 3PJ.",
        "phone": "You can contact them at 01223 503333."
    },
    "cheap_south": {
        "name": "The theater is called The Junction.",
        "address": "It is located at Clifton Way, CB1 7GX.",
        "phone": "You can contact them at 01223 511511."
    },
    "cheap_north": {
        "name": "The theater is called ADC Theatre.",
        "address": "It is located at Park Street, CB5 8AS.",
        "phone": "You can contact them at 01223 300085."
    },
    "expensive_north": {
        "name": "The theater is called Fitzwilliam Museum Auditorium.",
        "address": "It is located at Trumpington Street, CB2 1RB.",
        "phone": "You can contact them at 01223 332900."
    },
}

# Global variable to store the last three response contexts (key and type)
last_query_context = []

def update_context(key, query_type):
    """
    Updates the context history to store the last three queries.
    """
    global last_query_context
    # Add the new context to the history
    last_query_context.append({"key": key, "type": query_type})
    # Keep only the last three contexts
    if len(last_query_context) > 3:
        last_query_context.pop(0)

def get_response(query):
    """
    Determines whether the query is about a theater or restaurant, checks the direction and cost,
    and returns the appropriate response. Provides name, address, or phone number based on the query.
    """
    global last_query_context

    # Convert query to lowercase for case-insensitive checks
    query = query.lower()

    # Handle follow-up queries for address, phone, or name
    if "address" in query or "phone" in query or "contact" in query or "name" in query:
        for context in reversed(last_query_context):  # Check the most recent context first
            results = Restaurent_results if context["type"] == "restaurant" else Theater_results
            key = context["key"]
            if "address" in query:
                return results[key]["address"]
            elif "phone" in query or "contact" in query:
                return results[key]["phone"]
            elif "name" in query:
                return results[key]["name"]
        return "Sorry, I can only help with restaurants or theaters."

    # Determine if the query is about a restaurant or theater
    if "restaurant" in query:
        results = Restaurent_results
        query_type = "restaurant"
    elif "theater" in query:
        results = Theater_results
        query_type = "theater"
    else:
        return "Sorry, I can only help with restaurants or theaters."

    # Determine the cost and direction
    if "expensive" in query and "south" in query:
        key = "expensive_south"
    elif "cheap" in query and "south" in query:
        key = "cheap_south"
    elif "cheap" in query and "north" in query:
        key = "cheap_north"
    elif "expensive" in query and "north" in query:
        key = "expensive_north"
    else:
        return "Sorry, I couldn't understand the query. Please specify cost and direction."

    if key not in results:
        return "Sorry, no matching results found."

    # Update the context history
    update_context(key, query_type)

    # Return the name of the restaurant or theater
    return results[key]["name"]

def extract_information(response, info_type):
    """
    Extracts specific information (address or phone) from the full response.
    """
    if info_type == "address":
        start = response.find("located at")
        end = response.find("offering", start)
        return response[start:end].strip() if start != -1 and end != -1 else "Address information not available."
    elif info_type == "phone":
        start = response.find("contacted at")
        end = response.find("(postcode", start)
        return response[start:end].strip() if start != -1 and end != -1 else "Phone number information not available."
    return "Requested information not available."

query = "i need to find an expensive theater that's in the south section of the city. Please provide the address."
response = get_response(query)
print(response)