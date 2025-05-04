import streamlit as st
from Unitod_results import get_response

# Apply custom CSS for background image
st.markdown(
    """
    <style>
    .stApp {
        background-image: url('https://source.unsplash.com/random/1920x1080/?city');
        background-size: cover;
        background-position: center;
    }
    </style>
    """,
    unsafe_allow_html=True
)

# Title of the assistant app
st.title("Uni-ToD Assistant 🤖")

# Introduction
st.write("Welcome! Ask me about restaurants or theaters in the city, and I'll help you find the best options based on your preferences. "
         "You can also ask for specific details like the address or phone number.")

# Initialize session state for chat history
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []

# Input box for user query
user_query = st.text_input("Enter your query:")

# Generate response and update chat history
if user_query:
    response = get_response(user_query)
    st.session_state.chat_history.append({"user": user_query, "bot": response})

# Display chat history
st.write("### Conversation:")
for chat in st.session_state.chat_history:
    st.write(f"**You:**       {chat['user']}")
    st.write(f"**Assistant:**   {chat['bot']}")

# Thank-you note at the end of the conversation
if st.session_state.chat_history:
    st.write("Thank you for using the assistant! Have a great day!")