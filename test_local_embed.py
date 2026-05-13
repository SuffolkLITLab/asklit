from asklit.embeddings import get_embedding

def test():
    print("Testing local embedding...")
    text = "This is a test document."
    vector = get_embedding(text)
    print(f"Vector length: {len(vector)}")
    print(f"First 5 values: {vector[:5]}")
    if len(vector) > 0:
        print("SUCCESS: Local embedding generated.")

if __name__ == "__main__":
    test()
