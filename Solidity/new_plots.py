import pandas as pd
import matplotlib.pyplot as plt

# Load the data
csv_file = 'scythe_results.csv'  # Update this path to the actual CSV file location
df = pd.read_csv(csv_file)

# Calculate Scythe_Time_s + Perses_Time_s_Solidity2
df['Scythe_Perses_Time'] = df['Scythe_Time_s'] + df['Perses_Time_s_Solidity2']

# Calculate the speedup ratio
df['Speedup_Ratio'] = df['Perses_Time_s_Solidity'] / df['Scythe_Perses_Time']

# Calculate average values
average_speedup_ratio = df['Speedup_Ratio'].mean()
average_time_scythe_perses = df['Scythe_Perses_Time'].mean()
average_time_perses = df['Perses_Time_s_Solidity'].mean()
average_tokens_solidity2 = df['Perses_Tokens_Solidity2'].mean()
average_tokens_solidity = df['Perses_Tokens_Solidity'].mean()
average_tokens_diff = average_tokens_solidity - average_tokens_solidity2

# Calculate the average time difference
average_time_diff = average_time_perses - average_time_scythe_perses

# Print the average values
print(f"Average Speedup Ratio: {average_speedup_ratio:.2f}")
print(f"Average Time (Scythe + Perses_Solidity2): {average_time_scythe_perses:.2f} seconds")
print(f"Average Time (Perses_Solidity): {average_time_perses:.2f} seconds")
print(f"Average Tokens (Perses_Solidity2): {average_tokens_solidity2:.2f}")
print(f"Average Tokens (Perses_Solidity): {average_tokens_solidity:.2f}")
print(f"Average Token Difference: {average_tokens_diff:.2f}")
print(f"Average Time Difference: {average_time_diff:.2f} seconds")

# Plot: Scythe_Time_s + Perses_Time_s_Solidity2 vs Perses_Time_s_Solidity
plt.figure(figsize=(12, 6))

plt.subplot(1, 2, 1)
plt.plot(df['Directory'], df['Scythe_Perses_Time'], marker='o', linestyle='-', color='blue', label='Scythe_Time_s + Perses_Time_s_Solidity2')
plt.plot(df['Directory'], df['Perses_Time_s_Solidity'], marker='o', linestyle='-', color='red', label='Perses_Time_s_Solidity')
plt.xlabel('Directory')
plt.ylabel('Time (s)')
plt.title('Comparison of Times')
plt.xticks(rotation=90)
plt.legend()
plt.grid(True)

# Plot: Speedup Ratio
plt.subplot(1, 2, 2)
plt.plot(df['Directory'], df['Speedup_Ratio'], marker='o', linestyle='-', color='green', label='Speedup Ratio')
plt.xlabel('Directory')
plt.ylabel('Speedup Ratio')
plt.title('Speedup Ratio (Perses_Time_s_Solidity / (Scythe_Time_s + Perses_Time_s_Solidity2))')
plt.xticks(rotation=90)
plt.legend()
plt.grid(True)

plt.tight_layout()
plt.show()

# Plot: Perses_Tokens_Solidity2 vs Perses_Tokens_Solidity
plt.figure(figsize=(8, 6))
plt.plot(df['Directory'], df['Perses_Tokens_Solidity2'], marker='o', linestyle='-', color='purple', label='Perses_Tokens_Solidity2')
plt.plot(df['Directory'], df['Perses_Tokens_Solidity'], marker='o', linestyle='-', color='orange', label='Perses_Tokens_Solidity')
plt.xlabel('Directory')
plt.ylabel('Tokens')
plt.title('Comparison of Tokens')
plt.xticks(rotation=90)
plt.legend()
plt.grid(True)
plt.show()

