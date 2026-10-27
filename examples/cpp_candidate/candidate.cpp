#include <bits/stdc++.h>
using namespace std;

// Patience sorting, O(N log N). Bug: upper_bound counts equal values as increasing.
class Solution {
public:
    int lengthOfLIS(vector<int>& nums) {
        vector<int> tails;
        for (int x : nums) {
            auto it = upper_bound(tails.begin(), tails.end(), x);
            if (it == tails.end()) tails.push_back(x);
            else *it = x;
        }
        return (int)tails.size();
    }
};
