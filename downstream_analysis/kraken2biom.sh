### This script transforms the kraken2 reports into a biom file ###

in_path="/input/path/to/kraken2_reports/"
out_path="/output/path/to/biom_file/"

# If reports have 8 columns, transform to std format of 6 columns
filename="_report"
for file in "$in_path"*_report.txt; do
  if [ $(awk '{ print NF}' "$file" | head -1) = 8 ]
  then
    base=$(basename "$file" .txt)
    filename="_std"
    cut -f1-3,6-8 "$file" > "$in_path""$base""$filename".txt
    fi
done

# Run kraken-biom
kraken-biom "$in_path"*"$filename".txt -o "$out_path"kraken2.biom